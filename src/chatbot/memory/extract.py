"""Fact extraction: turn new user messages into scoped, versioned facts."""

import logging
from functools import cache
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from chatbot import prompts
from chatbot.agent.budget import call_context
from chatbot.agent.models import build_model
from chatbot.config import get_settings
from chatbot.memory import facts as fact_store
from chatbot.memory import messages as store
from chatbot.memory.models import Message, Scope

logger = logging.getLogger(__name__)


class ExtractedItem(BaseModel):
    scope: Literal["user", "thread", "ephemeral"]
    key: str = Field(description="short snake_case topic")
    text: str = Field(description="one self-contained sentence")


class Extraction(BaseModel):
    items: list[ExtractedItem] = Field(default_factory=list)


@cache
def get_extractor() -> Agent[None, Extraction]:
    return Agent(
        build_model(get_settings()),
        output_type=Extraction,
        instructions=prompts.load("extract"),
        # Each prompt is unique (new messages + current facts), so a cache write would never be
        # read back; skip the write surcharge. Other providers ignore this key.
        model_settings={"anthropic_cache": False},
    )


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + " …"


def build_prompt(known: list[str], batch: list[tuple[Message, str | None]], clip: int) -> str:
    known_block = "\n".join(known) if known else "(none)"
    msgs = []
    for msg, prev_reply in batch:
        if prev_reply:
            msgs.append(f"(assistant before turn {msg.turn}: {_clip(prev_reply, 300)})")
        msgs.append(f"turn {msg.turn} — user: {_clip(msg.text, clip)}")
    return f"Already remembered:\n{known_block}\n\nNew messages:\n" + "\n".join(msgs)


def _known_facts(user_id: str, thread_id: str, max_chars: int) -> list[str]:
    """Active facts as lines, newest kept when over `max_chars`, returned oldest first."""
    lines, used = [], 0
    for f in reversed(fact_store.active_facts(user_id, thread_id)):
        line = f"- [{f.scope}] {f.key}: {f.text}"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line)
    return lines[::-1]


async def extract_pending(user_id: str, thread_id: str) -> int:
    """Process every not-yet-extracted user message of this thread in bounded batches.

    Returns the number of facts stored.
    """
    settings = get_settings()
    stored = 0
    while batch := store.pending_for_extraction(thread_id, limit=settings.extract_batch_turns):
        known = _known_facts(user_id, thread_id, settings.extract_known_chars)
        prompt = build_prompt(known, batch, settings.extract_message_chars)
        with call_context(thread_id=thread_id, purpose="extract"):
            result = await get_extractor().run(prompt)

        last_turn = batch[-1][0].turn
        keep = [
            fact_store.NewFact(Scope(i.scope), i.key, i.text)
            for i in result.output.items
            if i.scope != "ephemeral"
        ]
        dropped = len(result.output.items) - len(keep)
        if dropped:
            logger.info("dropped %d ephemeral item(s) in %s", dropped, thread_id)
        stored += len(fact_store.apply(user_id, thread_id, last_turn, keep))
        store.mark_extracted([m.id for m, _ in batch if m.id is not None])
    return stored
