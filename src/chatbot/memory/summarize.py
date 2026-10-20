"""Rolling summaries: fixed chunks of older turns, each summarized once, never rewritten.

Chunks are summarized only after they are `summary_keep_recent_turns` behind the newest turn,
so the replayed window and the summaries rarely overlap. Fixed chunk boundaries keep the
summary block stable (cache-friendly) until the next chunk completes.
"""

from functools import cache

from pydantic_ai import Agent
from sqlmodel import col, func, select

from chatbot import prompts
from chatbot.agent.budget import call_context
from chatbot.agent.models import build_model
from chatbot.config import get_settings
from chatbot.memory import messages as store
from chatbot.memory.db import session
from chatbot.memory.models import Message, Role, Summary


@cache
def get_summarizer() -> Agent[None, str]:
    return Agent(
        build_model(get_settings()),
        instructions=prompts.load("summarize"),
        model_settings={"anthropic_cache": False},  # every prompt is unique
    )


def _last_summarized(thread_id: str) -> int:
    with session() as s:
        end = s.exec(select(func.max(Summary.end_turn)).where(Summary.thread_id == thread_id)).one()
    return end or 0


def _last_turn(thread_id: str) -> int:
    with session() as s:
        last = s.exec(select(func.max(Message.turn)).where(Message.thread_id == thread_id)).one()
    return last or 0


def chunk_prompt(
    start: int, end: int, rows_by_turn: dict[int, list[Message]], max_chars: int
) -> str:
    n_msgs = sum(len(r) for r in rows_by_turn.values()) or 1
    per_msg = max(120, max_chars // n_msgs)
    lines = [f"Turns {start}-{end}:"]
    for turn in sorted(rows_by_turn):
        for r in rows_by_turn[turn]:
            who = "user" if r.role == Role.USER else "assistant"
            text = " ".join(r.text.split())
            lines.append(f"[{turn}] {who}: {text[:per_msg]}")
    return "\n".join(lines)


async def summarize_pending(thread_id: str) -> int:
    """Summarize every complete chunk old enough. Returns the number of summaries written."""
    settings = get_settings()
    chunk, keep = settings.summary_chunk_turns, settings.summary_keep_recent_turns
    # Leave room for instructions and output under the budget.
    max_chars = int((settings.max_input_tokens // 2) * settings.estimate_chars_per_token)
    written = 0
    while True:
        start = _last_summarized(thread_id) + 1
        end = start + chunk - 1
        if _last_turn(thread_id) - end < keep:
            return written
        rows = store.turns(thread_id, list(range(start, end + 1)))
        with call_context(thread_id=thread_id, purpose="summarize"):
            result = await get_summarizer().run(chunk_prompt(start, end, rows, max_chars))
        with session() as s:
            s.add(Summary(thread_id=thread_id, start_turn=start, end_turn=end, text=result.output))
            s.commit()
        written += 1


def context_block(thread_id: str, *, before_turn: int) -> str:
    """The latest few summaries covering turns before the replayed window."""
    n = get_settings().summaries_in_context
    with session() as s:
        rows = s.exec(
            select(Summary)
            .where(Summary.thread_id == thread_id, Summary.start_turn < before_turn)
            .order_by(col(Summary.end_turn).desc())
            .limit(n)
        ).all()
    if not rows:
        return ""
    body = "\n".join(f"- (turns {r.start_turn}-{r.end_turn}) {r.text}" for r in reversed(rows))
    return "## Earlier in this conversation (summaries; older turns are searchable)\n" + body
