"""The single entry point shared by every surface (CLI, HTTP, web, Slack)."""

import asyncio
import atexit
import contextvars
import logging
import threading

from chatbot.agent.agent import ChatDeps, get_agent, reply_text
from chatbot.agent.budget import call_context
from chatbot.config import get_settings
from chatbot.memory import messages as store
from chatbot.memory.extract import extract_pending
from chatbot.memory.models import Source

SLACK_THREAD_ID = store.SLACK_THREAD_ID
logger = logging.getLogger(__name__)


async def _handle(
    user_id: str, thread_id: str, text: str, *, generate: bool, reply: str | None = None
) -> str | None:
    """One pipeline for live and ingested messages; ingest only skips the model reply."""
    settings = get_settings()
    store.ensure_thread(user_id, thread_id)

    run_json = None
    if generate:
        history = store.recent_history(
            thread_id,
            budget=settings.history_token_budget,
            step=settings.history_window_step,
            chars_per_token=settings.estimate_chars_per_token,
        )
        with call_context(thread_id=thread_id, purpose="chat"):
            result = await get_agent().run(
                text, message_history=history, deps=ChatDeps(user_id, thread_id)
            )
        reply = reply_text(result)
        run_json = result.new_messages_json().decode()

    store.record_turn(
        user_id,
        thread_id,
        text,
        reply,
        run_messages_json=run_json,
        source=Source.LIVE if generate else Source.INGEST,
    )

    # Facts are extracted after the reply but before returning, so the next message in any
    # thread sees them. Ingest batches its backlog to keep the number of extractor calls low.
    backlog = store.count_pending_extraction(thread_id)
    if generate or backlog >= settings.extract_batch_turns:
        try:
            await extract_pending(user_id, thread_id)
        except Exception:
            # Never lose the user's reply over memory upkeep; messages stay pending and are
            # retried on the next turn.
            logger.exception("fact extraction failed for %s", thread_id)
    return reply


async def flush_async(user_id: str, thread_id: str) -> None:
    """Finish background work for a thread (e.g. extraction left over after an ingest)."""
    await extract_pending(user_id, thread_id)


async def send_async(user_id: str, thread_id: str, text: str) -> str:
    """Handle one live user message and return the reply."""
    reply = await _handle(user_id, thread_id, text, generate=True)
    assert reply is not None
    return reply


async def ingest_async(user_id: str, thread_id: str, text: str, reply: str | None = None) -> None:
    """Load a historical message (and optionally the reply it got) without calling the model."""
    await _handle(user_id, thread_id, text, generate=False, reply=reply)


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


def ingest(user_id: str, thread_id: str, text: str, reply: str | None = None) -> None:
    _run_sync(ingest_async(user_id, thread_id, text, reply))


def flush(user_id: str, thread_id: str) -> None:
    _run_sync(flush_async(user_id, thread_id))
