"""The single entry point shared by every surface (CLI, HTTP, web, Slack)."""

import asyncio

from chatbot.agent.agent import get_agent
from chatbot.agent.budget import call_context
from chatbot.config import get_settings
from chatbot.memory import messages as store
from chatbot.memory.models import Source

SLACK_THREAD_ID = store.SLACK_THREAD_ID


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
            result = await get_agent().run(text, message_history=history)
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
    return reply


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
