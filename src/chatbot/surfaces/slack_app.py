"""Slack surface: a DM with the app is the endless `slack-dm` thread (Socket Mode, no tunnel).

    uv run chatbot slack     # needs SLACK_BOT_TOKEN (xoxb-) and SLACK_APP_TOKEN (xapp-)

Single user: every DM author maps to DEFAULT_USER_ID (hard-coded mapping, per the brief). If
SLACK_ALLOWED_USER is set, DMs from anyone else are ignored. Thin adapter over the core.
"""

import logging

from chatbot.config import get_settings
from chatbot.core import SLACK_THREAD_ID, send_async

logger = logging.getLogger(__name__)
_seen_events: dict[str, None] = {}  # Slack may redeliver an event; answer each once
_SEEN_MAX = 1000


def should_handle(event: dict, allowed_user: str | None) -> bool:
    """Plain DMs from a person (no bots, edits, joins), optionally from one allowed user."""
    if event.get("channel_type") != "im" or event.get("bot_id") or event.get("subtype"):
        return False
    if not (event.get("text") or "").strip():
        return False
    return not allowed_user or event.get("user") == allowed_user


def first_time(event_id: str | None) -> bool:
    if not event_id:
        return True
    if event_id in _seen_events:
        return False
    _seen_events[event_id] = None
    if len(_seen_events) > _SEEN_MAX:
        _seen_events.pop(next(iter(_seen_events)))
    return True


async def handle_dm(event: dict, *, event_id: str | None = None) -> str | None:
    """The reply to post for a DM event, or None if it should be ignored."""
    settings = get_settings()
    if not should_handle(event, settings.slack_allowed_user) or not first_time(event_id):
        return None
    return await send_async(settings.default_user_id, SLACK_THREAD_ID, event["text"].strip())


def run() -> None:
    import asyncio

    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    from slack_bolt.async_app import AsyncApp

    settings = get_settings()
    if not settings.slack_bot_token or not settings.slack_app_token:
        raise SystemExit("Set SLACK_BOT_TOKEN and SLACK_APP_TOKEN in .env")
    app = AsyncApp(token=settings.slack_bot_token.get_secret_value())

    @app.event("message")
    async def on_message(event, body, say):  # Bolt acks first (process_before_response=False)
        try:
            reply = await handle_dm(event, event_id=body.get("event_id"))
        except Exception:
            logger.exception("failed to answer a Slack DM")
            reply = "Sorry, something went wrong on my side. Please try again."
        if reply:
            await say(reply)

    async def main() -> None:
        handler = AsyncSocketModeHandler(app, settings.slack_app_token.get_secret_value())
        logger.info("Slack app connected (Socket Mode); DM it to chat")
        await handler.start_async()

    asyncio.run(main())
