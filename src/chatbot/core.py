"""The single entry point shared by every surface (CLI, HTTP, web, Slack)."""

import asyncio
import atexit
import contextvars
import logging
import threading
from dataclasses import dataclass, replace
from datetime import datetime

from pydantic_ai.messages import (
    CachePoint,
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters

from chatbot import prompts
from chatbot.agent.agent import ChatDeps, get_agent, reply_text, static_prompt_tokens
from chatbot.agent.budget import BudgetExceededError, call_context, estimate_tokens
from chatbot.capabilities import docs as doc_tools
from chatbot.config import get_settings
from chatbot.memory import facts, retrieve, search, summarize
from chatbot.memory import messages as store
from chatbot.memory.db import get_engine
from chatbot.memory.extract import extract_pending
from chatbot.memory.models import Source

SLACK_THREAD_ID = store.SLACK_THREAD_ID
logger = logging.getLogger(__name__)
_RETRY_SCALES = (1.0, 0.6, 0.35)


def _user_prompt(prompt: str, window: store.HistoryWindow) -> str | list:
    """The run's user prompt, with a cache breakpoint before it when there is history.

    `CachePoint()` first attaches `cache_control` to the end of the previous message, so
    tools + instructions + replayed history are cached and the next turn (whose history extends
    this one) reads them back. The current message itself (with its retrieved facts/snippets)
    never recurs, so it is deliberately left outside the cache. Without history there is nothing
    to attach to; the instructions breakpoint covers that case.
    """
    return [CachePoint(), prompt] if window.messages else prompt


def _restore_user_text(messages: list[ModelMessage], sent: str, original: str) -> list:
    """Store the run with what the user typed in place of the augmented prompt (and without the
    cache marker), so stored history holds no retrieved snippets (smaller; forget stays simple).

    Instructions are dropped too: they are rebuilt every turn and only the latest are sent, so a
    stored copy would only waste space and keep preferences/summaries in more places."""

    def is_sent(content) -> bool:
        if isinstance(content, list):
            return sent in content
        return content == sent

    out = []
    for m in messages:
        if isinstance(m, ModelRequest):
            parts = [
                replace(p, content=original)
                if isinstance(p, UserPromptPart) and is_sent(p.content)
                else p
                for p in m.parts
            ]
            m = replace(m, parts=parts, instructions=None)
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


@dataclass
class _Context:
    preferences: str
    summaries: str
    facts: str
    snippets: str
    history_budget: int


def _render_prompt(text: str, ctx: _Context) -> str:
    sections = []
    if ctx.facts:
        sections.append(prompts.load("facts_header") + "\n" + ctx.facts)
    if ctx.snippets:
        sections.append(prompts.load("snippets_header") + "\n" + ctx.snippets)
    if not sections:
        return text
    return (
        prompts.load("retrieved")
        .replace("{sections}", "\n\n".join(sections))
        .replace("{message}", text)
    )


def _assemble(user_id: str, thread_id: str, text: str, scale: float = 1.0):
    """Build prompt, history window and deps for a chat turn, shrinking until the estimate fits.

    Each source has its own budget, so this normally fits first time; the loop is a safety net
    (e.g. a huge pasted message) that degrades context instead of failing the turn. The budget
    guard on the model still has the final say.
    """
    settings = get_settings()
    cpt = settings.estimate_chars_per_token
    target = int(settings.context_target_tokens * scale)
    system_tokens = static_prompt_tokens() + settings.tool_overhead_tokens

    def window_for(budget: int):
        return store.recent_history(
            thread_id, budget=budget, step=settings.history_window_step, chars_per_token=cpt
        )

    ctx = _Context(
        preferences=facts.render_preferences(user_id, thread_id),
        summaries="",
        facts=facts.render_relevant_facts(
            user_id, thread_id, text, int(settings.facts_token_budget * cpt)
        ),
        snippets="",
        history_budget=settings.history_token_budget,
    )
    window = window_for(ctx.history_budget)
    ctx.summaries = summarize.context_block(thread_id, before_turn=window.start_turn)
    ctx.snippets = retrieve.snippets(thread_id, text, before_turn=window.start_turn)

    def estimate() -> int:
        strings = ctx.preferences + ctx.summaries + _render_prompt(text, ctx)
        history = estimate_tokens(window.messages, ModelRequestParameters(), cpt)
        return int(system_tokens + len(strings) / cpt + history)

    shrink_steps = [
        lambda: setattr(ctx, "snippets", ""),
        lambda: setattr(ctx, "summaries", ""),
        lambda: setattr(ctx, "history_budget", ctx.history_budget // 2),
        lambda: setattr(ctx, "facts", ""),
        lambda: setattr(ctx, "history_budget", 0),
    ]
    for step in shrink_steps:
        if estimate() <= target:
            break
        logger.warning(
            "context over target (%d > %d) in %s; shrinking", estimate(), target, thread_id
        )
        step()
        window = window_for(ctx.history_budget)

    deps = ChatDeps(
        user_id,
        thread_id,
        preferences=ctx.preferences,
        summaries=ctx.summaries,
        docs=doc_tools.refs_block(thread_id),
    )
    return _render_prompt(text, ctx), window, deps


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
    search.repair_once(get_engine())  # heal index rows lost to a crash after a message commit
    store.ensure_thread(user_id, thread_id)

    run_json = None
    if generate:
        # Our estimate can't know each provider's real count (Meta's overcounts ~2x), so if
        # the budget guard refuses a request before sending, rebuild a smaller context.
        for scale in _RETRY_SCALES:
            prompt, window, deps = _assemble(user_id, thread_id, text, scale)
            try:
                with call_context(thread_id=thread_id, purpose="chat"):
                    result = await get_agent().run(
                        _user_prompt(prompt, window), message_history=window.messages, deps=deps
                    )
                break
            except BudgetExceededError as exc:
                if exc.stage != "preflight" or scale == _RETRY_SCALES[-1]:
                    raise
                logger.warning("chat context refused (%s); retrying smaller", exc)
        # A tool may fix the reply (e.g. a validated LinkedIn post) so the model can't alter it.
        reply = deps.outputs.get("reply") or reply_text(result)
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
