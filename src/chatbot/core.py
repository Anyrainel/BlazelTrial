"""The single entry point shared by every surface (CLI, HTTP, web, Slack)."""

import asyncio

from chatbot.agent.agent import get_agent
from chatbot.agent.budget import call_context

SLACK_THREAD_ID = "slack-dm"


async def send_async(user_id: str, thread_id: str, text: str) -> str:
    """Handle one user message and return the reply.

    Milestone 1: stateless — each call sees only `text`. Memory and history arrive in milestone 2+.
    """
    with call_context(thread_id=thread_id, purpose="chat"):
        result = await get_agent().run(text)
    return result.output


def send(user_id: str, thread_id: str, text: str) -> str:
    """Sync wrapper for callers without an event loop (CLI, scripts, scenario runner)."""
    return asyncio.run(send_async(user_id, thread_id, text))
