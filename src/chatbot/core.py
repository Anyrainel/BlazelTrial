"""The single entry point shared by every surface (CLI, HTTP, web, Slack)."""

import asyncio
import atexit
import contextvars
import logging
import threading
from dataclasses import replace
from datetime import datetime
from weakref import WeakKeyDictionary

from pydantic_ai.messages import (
    CachePoint,
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters

from chatbot.agent.agent import ChatDeps, get_agent, reply_text, static_prompt_tokens
from chatbot.agent.budget import BudgetExceededError, call_context, estimate_tokens
from chatbot.config import get_settings
from chatbot.memory import forget, notes, refs, search
from chatbot.memory import messages as store
from chatbot.memory.db import get_engine
from chatbot.memory.models import Source

SLACK_THREAD_ID = store.SLACK_THREAD_ID
logger = logging.getLogger(__name__)
_RETRY_SCALES = (1.0, 0.6, 0.35)


def _user_prompt(text: str, window: store.HistoryWindow) -> str | list:
    """The run's user prompt, with a cache breakpoint before it when there is history.

    `CachePoint()` first attaches `cache_control` to the end of the previous message, so
    tools + instructions + replayed history are cached and the next turn (whose history extends
    this one) reads them back. Without history there is nothing to attach to; the instructions
    breakpoint covers that case.
    """
    return [CachePoint(), text] if window.messages else text


def _for_storage(messages: list[ModelMessage], text: str) -> list:
    """The run as stored for replay: the plain user text (no cache marker) and no instructions
    (rebuilt every turn; only the latest are ever sent)."""
    out = []
    for m in messages:
        if isinstance(m, ModelRequest):
            parts = [
                replace(p, content=text)
                if isinstance(p, UserPromptPart) and isinstance(p.content, list)
                else p
                for p in m.parts
            ]
            m = replace(m, parts=parts, instructions=None)
        out.append(m)
    return out


async def _upkeep(thread_id: str) -> None:
    """Memory maintenance after a turn: source-linked episode notes for settled segments.

    The same path serves live and ingested history. Failures are logged, never raised (the
    reply must not be lost over upkeep); pending segments are retried on the next turn.
    """
    try:
        await notes.write_pending(thread_id)
    except Exception:
        logger.exception("note maintenance failed for %s", thread_id)


def _window(thread_id: str, turn: int, scale: float) -> store.HistoryWindow:
    """Recent turns before `turn`, shrunk until the estimated request fits the target.

    No memory is injected: facts, preferences, notes and older messages are reached through
    the memory tools. The loop is a safety net (e.g. a huge pasted message); the budget guard on
    the model still has the final say.
    """
    settings = get_settings()
    cpt = settings.estimate_chars_per_token
    target = int(settings.context_target_tokens * scale)
    fixed = static_prompt_tokens() + settings.tool_overhead_tokens
    budget = settings.history_token_budget
    while True:
        window = store.recent_history(
            thread_id,
            budget=budget,
            step=settings.history_window_step,
            chars_per_token=cpt,
            before_turn=turn,
        )
        used = fixed + estimate_tokens(window.messages, ModelRequestParameters(), cpt)
        if used <= target or budget == 0:
            return window
        logger.warning("history over target (%d > %d) in %s; shrinking", used, target, thread_id)
        budget = budget // 2 if budget > 200 else 0


async def _reply(user_id: str, thread_id: str, user_msg) -> tuple[str, str, list[str]]:
    """Run the chat agent for a stored user message; returns (reply, run JSON, values the
    turn asked to forget)."""
    # Tool side effects recorded during the turn (values forgotten, a verbatim reply) must
    # survive a retry: the first attempt's tools may already have run.
    outputs: dict = {}
    for scale in _RETRY_SCALES:
        window = _window(thread_id, user_msg.turn, scale)
        deps = ChatDeps(user_id, thread_id, current_msg_ref=refs.msg(user_msg.id), outputs=outputs)
        try:
            with call_context(thread_id=thread_id, purpose="chat"):
                result = await get_agent().run(
                    _user_prompt(user_msg.text, window),
                    message_history=window.messages,
                    deps=deps,
                )
            break
        except BudgetExceededError as exc:
            # Our estimate can't know each provider's real count, so if the guard refuses a
            # request before sending, retry with less history.
            if exc.stage != "preflight" or scale == _RETRY_SCALES[-1]:
                raise
            logger.warning("chat context refused (%s); retrying smaller", exc)
    # A tool may fix the reply (e.g. a validated LinkedIn post) so the model can't alter it.
    reply = deps.outputs.get("reply") or reply_text(result)
    forgotten = deps.outputs.get("forget", [])
    if get_settings().memory_forget_mode == "hard":
        reply = forget.redact(reply, forgotten)
    stored = _for_storage(result.new_messages(), user_msg.text)
    return reply, ModelMessagesTypeAdapter.dump_json(stored).decode(), forgotten


# One turn at a time per conversation, whatever the surface: a second message in the same
# thread waits for the first, so replies keep their order and memory writes never interleave.
_turn_locks: WeakKeyDictionary = WeakKeyDictionary()


async def _handle(
    user_id: str,
    thread_id: str,
    text: str,
    *,
    generate: bool,
    reply: str | None = None,
    at: datetime | None = None,
    request_id: str | None = None,
) -> str | None:
    """One pipeline for live and ingested messages; ingest only skips the model reply.

    The user message is stored (and indexed) before the model runs, so memory writes during
    the turn can cite it as their source.
    """
    locks = _turn_locks.setdefault(asyncio.get_running_loop(), {})
    async with locks.setdefault(thread_id, asyncio.Lock()):
        return await _handle_locked(user_id, thread_id, text, generate, reply, at, request_id)


async def _handle_locked(
    user_id: str,
    thread_id: str,
    text: str,
    generate: bool,
    reply: str | None,
    at: datetime | None,
    request_id: str | None = None,
) -> str | None:
    search.repair_once(get_engine())  # heal index rows lost to a crash after a message commit
    store.ensure_thread(user_id, thread_id)
    source = Source.LIVE if generate else Source.INGEST
    rows = store.record_turn(
        user_id,
        thread_id,
        text,
        None if generate else reply,
        source=source,
        at=at,
        request_id=request_id,
    )
    if generate and request_id is not None:
        previous = store.turn_reply(rows[0])
        if previous is not None:
            return store.FORGOTTEN_REPLY if previous.forgotten else previous.text
    search.index(rows)
    if generate:
        reply, run_json, forgotten = await _reply(user_id, thread_id, rows[0])
        search.index(
            [store.add_reply(rows[0], reply, run_messages_json=run_json, request_id=request_id)]
        )
        if forgotten:
            # This turn's own run (tool args, memory answers) can hold the values: sweep again
            # now that it is stored, then purge the bytes from disk.
            forget.forget_values(user_id, forgotten)
    await _upkeep(thread_id)
    return reply


async def flush_async(user_id: str, thread_id: str) -> None:
    """Finish pending maintenance for a thread (e.g. notes left over after an ingest)."""
    await _upkeep(thread_id)


async def send_async(
    user_id: str, thread_id: str, text: str, *, request_id: str | None = None
) -> str:
    """Handle one live user message and return the reply."""
    reply = await _handle(user_id, thread_id, text, generate=True, request_id=request_id)
    assert reply is not None
    return reply


async def ingest_async(
    user_id: str,
    thread_id: str,
    text: str,
    reply: str | None = None,
    at: datetime | None = None,
) -> None:
    """Load a historical message (optionally its reply and timestamp) without a model reply."""
    await _handle(user_id, thread_id, text, generate=False, reply=reply, at=at)


# Sync wrappers share ONE event loop for the whole process. A fresh `asyncio.run()` per call
# closes its loop, while cached provider clients keep pooled connections bound to it, so the
# next call fails with "Event loop is closed".
_runner: asyncio.Runner | None = None
_runner_lock = threading.Lock()


def _run_sync(coro):
    global _runner
    with _runner_lock:
        if _runner is None:
            _runner = asyncio.Runner()
            atexit.register(_runner.close)
        # Run in the caller's current context; the Runner would otherwise reuse the context
        # captured on its first call (losing later context-var changes such as overrides).
        return _runner.run(coro, context=contextvars.copy_context())


def send(user_id: str, thread_id: str, text: str) -> str:
    """Sync wrapper for callers without an event loop (CLI, scripts, scenario runner)."""
    return _run_sync(send_async(user_id, thread_id, text))


def ingest(
    user_id: str,
    thread_id: str,
    text: str,
    reply: str | None = None,
    at: datetime | None = None,
) -> None:
    _run_sync(ingest_async(user_id, thread_id, text, reply, at))


def flush(user_id: str, thread_id: str) -> None:
    _run_sync(flush_async(user_id, thread_id))
