"""Messaging: `send_message` records the message instead of delivering it."""

import logging
import threading

from pydantic_ai import RunContext
from sqlmodel import col, select

from chatbot.capabilities.memory import mutation_key
from chatbot.capabilities.registry import capability
from chatbot.memory.db import session
from chatbot.memory.models import OutboundMessage

logger = logging.getLogger(__name__)
_send_lock = threading.Lock()

_MAX_TEXT_CHARS = 4000


def send_message(ctx: RunContext, to: str, text: str) -> str:
    """Record a message on the user's behalf; delivery is simulated.

    Args:
        to: The recipient's name.
        text: The complete message text.
    """
    to, text = to.strip(), text.strip()
    if not to or not text:
        return "Not sent: both a recipient and a message text are required."
    if len(text) > _MAX_TEXT_CHARS:
        return f"Not sent: the message is over {_MAX_TEXT_CHARS} characters; shorten it."
    key = mutation_key(ctx, "send_message", to, text)
    with _send_lock, session() as s:
        if s.exec(select(OutboundMessage).where(OutboundMessage.idempotency_key == key)).first():
            return f"Message to {to} was already recorded in this turn; not sent again."
        s.add(
            OutboundMessage(
                thread_id=ctx.deps.thread_id, recipient=to, text=text, idempotency_key=key
            )
        )
        s.commit()
    logger.info("send_message to=%r chars=%d", to, len(text))
    return f"Message to {to} recorded (delivery is simulated)."


def sent_messages(thread_id: str | None = None) -> list[OutboundMessage]:
    """Recorded messages, oldest first (for tests, scenarios and inspection)."""
    with session() as s:
        query = select(OutboundMessage).order_by(col(OutboundMessage.id))
        if thread_id is not None:
            query = query.where(OutboundMessage.thread_id == thread_id)
        return list(s.exec(query).all())


CAPABILITY = capability("messaging", "Send (record) a message to a person.", __file__, send_message)
