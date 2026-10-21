"""Note maintenance: source-linked episode notes over fixed, settled segments of a session.

Shared by live and ingested history (core upkeep). A segment = `summary_chunk_turns` turns,
written only once it is `summary_keep_recent_turns` behind the newest turn. Notes go through
`records.add_note` (exact covered messages, non-overlapping, indexed in the same transaction).
They are searchable through ask_memory and never injected into prompts.
"""

from functools import cache

from pydantic_ai import Agent
from sqlmodel import func, select

from chatbot import prompts
from chatbot.agent.budget import call_context
from chatbot.agent.models import build_model
from chatbot.config import get_settings
from chatbot.memory import messages as store
from chatbot.memory import records
from chatbot.memory.db import session
from chatbot.memory.models import EpisodeNote, Message, Role


@cache
def get_note_writer() -> Agent[None, str]:
    return Agent(
        build_model(get_settings()),
        instructions=prompts.load("summarize"),
        model_settings={"anthropic_cache": False},  # every prompt is unique
    )


def _covered_turns(thread_id: str) -> int:
    """Highest turn whose messages are already covered by a note (0 if none)."""
    with session() as s:
        end = s.exec(
            select(func.max(EpisodeNote.end_message_id)).where(EpisodeNote.session_id == thread_id)
        ).one()
        if not end:
            return 0
        return s.get(Message, end).turn


def _last_turn(thread_id: str) -> int:
    with session() as s:
        last = s.exec(select(func.max(Message.turn)).where(Message.thread_id == thread_id)).one()
    return last or 0


def segment_prompt(
    start: int, end: int, rows_by_turn: dict[int, list[Message]], max_chars: int
) -> str:
    n_msgs = sum(len(r) for r in rows_by_turn.values()) or 1
    per_msg = max(120, max_chars // n_msgs)
    lines = [f"Turns {start}-{end}:"]
    for turn in sorted(rows_by_turn):
        for r in rows_by_turn[turn]:
            who = "user" if r.role == Role.USER else "assistant"
            lines.append(f"[{turn}] {who}: {' '.join(r.text.split())[:per_msg]}")
    return "\n".join(lines)


async def write_pending(thread_id: str) -> int:
    """Write notes for every settled, uncovered segment. Returns the number written."""
    settings = get_settings()
    chunk, keep = settings.summary_chunk_turns, settings.summary_keep_recent_turns
    # Leave room for instructions and output under the budget.
    max_chars = int((settings.max_input_tokens // 2) * settings.estimate_chars_per_token)
    written = 0
    while True:
        start = _covered_turns(thread_id) + 1
        end = start + chunk - 1
        if _last_turn(thread_id) - end < keep:
            return written
        rows = store.turns(thread_id, list(range(start, end + 1)))
        ids = [m.id for turn in sorted(rows) for m in rows[turn]]
        if not ids:
            return written
        with call_context(thread_id=thread_id, purpose="note"):
            result = await get_note_writer().run(segment_prompt(start, end, rows, max_chars))
        user_id = rows[min(rows)][0].user_id
        records.add_note(user_id, thread_id, ids, result.output[: records.MAX_NOTE])
        written += 1
