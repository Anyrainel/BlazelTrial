"""The single entry point shared by every surface (CLI, HTTP, web, Slack)."""

import asyncio
import logging

from chatbot.agent.agent import ChatDeps, get_agent
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
        reply = result.output
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


def send(user_id: str, thread_id: str, text: str) -> str:
    """Sync wrapper for callers without an event loop (CLI, scripts, scenario runner)."""
    return asyncio.run(send_async(user_id, thread_id, text))


def ingest(user_id: str, thread_id: str, text: str, reply: str | None = None) -> None:
    asyncio.run(ingest_async(user_id, thread_id, text, reply))


def flush(user_id: str, thread_id: str) -> None:
    asyncio.run(flush_async(user_id, thread_id))
