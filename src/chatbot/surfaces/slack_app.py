"""Slack DM and explicit channel mentions: thin, serialized adapters over the core."""

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from weakref import WeakKeyDictionary

from sqlmodel import col, select

from chatbot.config import get_settings
from chatbot.core import SLACK_THREAD_ID, send_async
from chatbot.memory.db import session
from chatbot.memory.models import Message, Role, SlackDelivery

logger = logging.getLogger(__name__)
# One Socket Mode worker per state DB. Locks span generation AND delivery, so replies
# cannot overtake each other. Separate channels may proceed concurrently.
_locks: WeakKeyDictionary = WeakKeyDictionary()


def conversation_id(event: dict) -> str:
    return SLACK_THREAD_ID if event.get("channel_type") == "im" else "slack-" + event["channel"]


def should_handle(event: dict, allowed_user: str | None, bot_user_id: str = "") -> bool:
    """Human plain DMs or app_mention events; never general channel message traffic."""
    if (
        event.get("bot_id")
        or event.get("subtype")
        or event.get("hidden")
        or not event.get("user")
        or event.get("user") == bot_user_id
    ):
        return False
    if allowed_user and event["user"] != allowed_user:
        return False
    is_dm = event.get("channel_type") == "im" and event.get("type", "message") == "message"
    is_mention = (
        event.get("type") == "app_mention"
        and event.get("channel_type") not in ("im", "mpim")
        and bool(event.get("channel"))
        and bool(bot_user_id)
        and bool(
            re.search(r"<@" + re.escape(bot_user_id) + r"(?:\|[^>]+)?>", event.get("text", ""))
        )
    )
    return (is_dm or is_mention) and bool(normalize_text(event.get("text", ""), bot_user_id))


def normalize_text(text: str, bot_user_id: str) -> str:
    """Remove only Hazel's Slack mention tokens, preserving other users and links."""
    if bot_user_id:
        text = re.sub(r"<@" + re.escape(bot_user_id) + r"(?:\|[^>]+)?>", "", text)
    return text.strip()


def slack_format(text: str) -> str:
    """Conservative mrkdwn conversion; keep fenced/inline code verbatim."""

    def prose(value: str) -> str:
        value = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        value = re.sub(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)", r"<\2|\1>", value)
        value = re.sub(r"\*\*(.+?)\*\*", r"*\1*", value)
        value = re.sub(r"(?m)^#{1,6}\s+(.+)$", r"*\1*", value)
        # Tables become readable rows; skip the alignment separator.
        rows = []
        for line in value.splitlines():
            if re.fullmatch(r"\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*", line):
                continue
            if line.strip().startswith("|") and line.strip().endswith("|"):
                line = " • ".join(cell.strip() for cell in line.strip().strip("|").split("|"))
            rows.append(line)
        return "\n".join(rows)

    return "".join(
        part if part.startswith("`") else prose(part)
        for part in re.split(r"(```[\s\S]*?```|`[^`\n]+`)", text)
    )


async def handle_dm(event: dict, *, event_id: str | None = None) -> str | None:
    """Compatibility helper for callers asking only for a DM core reply."""
    settings = get_settings()
    if not should_handle(event, settings.slack_allowed_user):
        return None
    return await send_async(settings.default_user_id, SLACK_THREAD_ID, event["text"].strip())


async def handle_event(
    event: dict,
    *,
    body: dict,
    bot_user_id: str,
    post: Callable[..., Awaitable],
    set_status: Callable[..., Awaitable] | None = None,
    attempts: int = 1,
) -> None:
    """Persist generated output before posting; delivery retries reuse it after restarts.

    No text is duplicated in the outbox, so forgetting removes pending output too.
    Exceptions propagate to the listener's retry loop; a failed event is never completed.
    """
    settings = get_settings()
    if not should_handle(event, settings.slack_allowed_user, bot_user_id):
        return
    channel, ts = event.get("channel"), event.get("ts")
    if not channel or not ts:
        return  # real Slack events always supply these; no unsafe fallback dedup key
    tid = conversation_id(event)
    key = ":".join((body.get("team_id", ""), channel, ts))
    locks = _locks.setdefault(asyncio.get_running_loop(), {})
    async with locks.setdefault(tid, asyncio.Lock()):
        with session() as s:
            delivery = s.get(SlackDelivery, key)
        if delivery is not None and delivery.delivered:
            return
        async with _working_status(event, set_status):
            # Keep status and ordering through generation, posting and retries.
            for attempt in range(attempts):
                try:
                    await _deliver_event(event, key=key, bot_user_id=bot_user_id, post=post)
                    return
                except Exception:
                    logger.exception("Slack event failed (attempt %d/%d)", attempt + 1, attempts)
                    if attempt + 1 == attempts:
                        raise
                    await asyncio.sleep(2**attempt)


@asynccontextmanager
async def _working_status(
    event: dict, set_status: Callable[..., Awaitable] | None
) -> AsyncIterator[None]:
    """Refresh Slack's two-minute status until delivery; always attempt cleanup."""
    if set_status is None:
        yield
        return

    async def update(status: str) -> None:
        try:
            await set_status(
                channel_id=event["channel"],
                thread_ts=event.get("thread_ts") or event["ts"],
                status=status,
            )
        except Exception:
            logger.warning("Slack working status update failed", exc_info=True)

    async def refresh() -> None:
        while True:
            await asyncio.sleep(60)
            await update("is thinking…")

    try:
        await update("is thinking…")
        task = asyncio.create_task(refresh())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
    finally:
        await update("")


async def _deliver_event(
    event: dict,
    *,
    key: str,
    bot_user_id: str,
    post: Callable[..., Awaitable],
) -> None:
    settings = get_settings()
    tid = conversation_id(event)
    channel, ts = event["channel"], event["ts"]
    with session() as s:
        delivery = s.get(SlackDelivery, key)
    if delivery is not None and delivery.delivered:
        return
    if delivery is None:
        await send_async(settings.default_user_id, tid, normalize_text(event["text"], bot_user_id))
        with session() as s:
            reply = s.exec(
                select(Message)
                .where(
                    Message.thread_id == tid,
                    Message.role == Role.ASSISTANT,
                )
                .order_by(col(Message.id).desc())
                .limit(1)
            ).one()
            delivery = SlackDelivery(id=key, reply_message_id=reply.id)
            s.add(delivery)
            s.commit()
    with session() as s:
        reply = s.get(Message, delivery.reply_message_id) if delivery.reply_message_id else None
    if reply is not None:
        parent = event.get("thread_ts") or (ts if tid != SLACK_THREAD_ID else None)
        await post(
            channel=channel,
            text=slack_format(reply.text),
            thread_ts=parent,
            unfurl_links=False,
            unfurl_media=False,
        )
    # A reply deleted by forget must never be regenerated or delivered.
    with session() as s:
        delivery.delivered = True
        s.add(delivery)
        s.commit()


def run() -> None:
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    from slack_bolt.async_app import AsyncApp

    settings = get_settings()
    if not settings.slack_bot_token or not settings.slack_app_token:
        raise SystemExit("Set SLACK_BOT_TOKEN and SLACK_APP_TOKEN in .env")
    app = AsyncApp(token=settings.slack_bot_token.get_secret_value())

    @app.event("app_mention")
    @app.event("message")
    async def on_message(event, body, client, context):
        # Socket Mode acknowledges before processing. Explicit bounded retries are
        # necessary: Slack will not redeliver an already acknowledged model failure.
        try:
            await handle_event(
                event,
                body=body,
                bot_user_id=context.bot_user_id,
                post=client.chat_postMessage,
                set_status=client.assistant_threads_setStatus,
                attempts=3,
            )
        except Exception:
            logger.exception("Slack event exhausted retries; event_id=%s", body.get("event_id"))

    @app.event("app_home_opened")
    async def on_home(event):
        pass

    async def main() -> None:
        handler = AsyncSocketModeHandler(app, settings.slack_app_token.get_secret_value())
        logger.info("Slack worker source=%s; temporary thread status enabled", __file__)
        await handler.start_async()

    asyncio.run(main())
