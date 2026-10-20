"""The single entry point shared by every surface (CLI, HTTP, web, Slack)."""

import asyncio
import atexit
import contextvars
import logging
import threading
from dataclasses import replace
from datetime import datetime

from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    UserPromptPart,
)

from chatbot import prompts
from chatbot.agent.agent import ChatDeps, get_agent, reply_text
from chatbot.agent.budget import call_context
from chatbot.config import get_settings
from chatbot.memory import messages as store
from chatbot.memory import retrieve, search, summarize
from chatbot.memory.extract import extract_pending
from chatbot.memory.models import Source

SLACK_THREAD_ID = store.SLACK_THREAD_ID
logger = logging.getLogger(__name__)


def _restore_user_text(messages: list[ModelMessage], sent: str, original: str) -> list:
    """Replace the retrieval-augmented prompt with what the user typed before storing the run,
    so stored history holds no copies of retrieved snippets (smaller, and forget stays simple)."""
    if sent == original:
        return messages
    out = []
    for m in messages:
        if isinstance(m, ModelRequest):
            parts = [
                replace(p, content=original)
                if isinstance(p, UserPromptPart) and p.content == sent
                else p
                for p in m.parts
            ]
            m = replace(m, parts=parts)
        out.append(m)
    return out


async def _upkeep(user_id: str, thread_id: str, *, force: bool) -> None:
    """Memory maintenance after a turn: fact extraction and rolling summaries.

    Failures are logged, never raised: the user's reply must not be lost over upkeep, and
    pending work is retried on the next turn.
    """
    settings = get_settings()
    if force or store.count_pending_extraction(thread_id) >= settings.extract_batch_turns:
        try:
            await extract_pending(user_id, thread_id)
        except Exception:
            logger.exception("fact extraction failed for %s", thread_id)
    try:
        await summarize.summarize_pending(thread_id)
    except Exception:
        logger.exception("summarization failed for %s", thread_id)


async def _handle(
    user_id: str,
    thread_id: str,
    text: str,
    *,
    generate: bool,
    reply: str | None = None,
    at: datetime | None = None,
) -> str | None:
    """One pipeline for live and ingested messages; ingest only skips the model reply."""
    settings = get_settings()
    store.ensure_thread(user_id, thread_id)

    run_json = None
    if generate:
        window = store.recent_history(
            thread_id,
            budget=settings.history_token_budget,
            step=settings.history_window_step,
            chars_per_token=settings.estimate_chars_per_token,
        )
        found = retrieve.snippets(thread_id, text, before_turn=window.start_turn)
        prompt = (
            prompts.load("retrieved").replace("{snippets}", found).replace("{message}", text)
            if found
            else text
        )
        deps = ChatDeps(
            user_id,
            thread_id,
            summaries=summarize.context_block(thread_id, before_turn=window.start_turn),
        )
        with call_context(thread_id=thread_id, purpose="chat"):
            result = await get_agent().run(prompt, message_history=window.messages, deps=deps)
        reply = reply_text(result)
        stored = _restore_user_text(result.new_messages(), prompt, text)
        run_json = ModelMessagesTypeAdapter.dump_json(stored).decode()

    rows = store.record_turn(
        user_id,
        thread_id,
        text,
        reply,
        run_messages_json=run_json,
        source=Source.LIVE if generate else Source.INGEST,
        at=at,
    )
    search.index(rows)

    # Upkeep runs before returning, so the next message in any thread sees new facts. Live
    # turns extract immediately; ingest batches its backlog to limit extractor calls.
    await _upkeep(user_id, thread_id, force=generate)
    return reply


async def flush_async(user_id: str, thread_id: str) -> None:
    """Finish pending upkeep for a thread (e.g. extraction left over after an ingest)."""
    await _upkeep(user_id, thread_id, force=True)


async def send_async(user_id: str, thread_id: str, text: str) -> str:
    """Handle one live user message and return the reply."""
    reply = await _handle(user_id, thread_id, text, generate=True)
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
