"""Message store: persist turns and rebuild a bounded, cache-friendly history window."""

from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters
from sqlmodel import col, func, select

from chatbot.agent.budget import estimate_tokens
from chatbot.memory.db import session
from chatbot.memory.models import Message, Role, Source, Thread, ThreadKind

SLACK_THREAD_ID = "slack-dm"


def ensure_thread(user_id: str, thread_id: str) -> Thread:
    with session() as s:
        thread = s.get(Thread, thread_id)
        if thread is None:
            kind = ThreadKind.SLACK if thread_id == SLACK_THREAD_ID else ThreadKind.SESSION
            thread = Thread(id=thread_id, user_id=user_id, kind=kind)
            s.add(thread)
            s.commit()
        return thread


def record_turn(
    user_id: str,
    thread_id: str,
    user_text: str,
    reply_text: str | None,
    *,
    run_messages_json: str | None = None,
    source: Source = Source.LIVE,
) -> int:
    """Store one turn atomically (`reply_text=None`: user message only). Returns the turn no."""
    with session() as s:
        last = s.exec(select(func.max(Message.turn)).where(Message.thread_id == thread_id)).one()
        turn = (last or 0) + 1
        s.add(
            Message(
                thread_id=thread_id,
                user_id=user_id,
                turn=turn,
                role=Role.USER,
                text=user_text,
                source=source,
            )
        )
        if reply_text is not None:
            s.add(
                Message(
                    thread_id=thread_id,
                    user_id=user_id,
                    turn=turn,
                    role=Role.ASSISTANT,
                    text=reply_text,
                    source=source,
                    run_messages_json=run_messages_json,
                )
            )
        s.commit()
        return turn


def _turn_messages(rows: list[Message]) -> list[ModelMessage]:
    """PydanticAI messages for one turn: the stored run if we have it, else plain text."""
    reply = next((r for r in rows if r.role == Role.ASSISTANT), None)
    if reply is not None and reply.run_messages_json:
        return ModelMessagesTypeAdapter.validate_json(reply.run_messages_json)
    out: list[ModelMessage] = []
    for r in rows:
        if r.role == Role.USER:
            out.append(ModelRequest(parts=[UserPromptPart(content=r.text)]))
        else:
            out.append(ModelResponse(parts=[TextPart(content=r.text)]))
    return out


def choose_window_start(turn_tokens: list[tuple[int, int]], budget: int, step: int) -> int | None:
    """Pick the first turn to replay.

    `turn_tokens` is [(turn_no, tokens)] in ascending turn order. The window holds as many recent
    turns as fit in `budget`, but its start snaps forward to a turn number of the form k*step+1,
    so it only moves every `step` turns and the replayed prefix stays identical (cacheable) in
    between. Returns None when not even the latest turn fits.
    """
    total, earliest = 0, None
    for turn, tokens in reversed(turn_tokens):
        if total + tokens > budget:
            break
        total += tokens
        earliest = turn
    if earliest is None:
        return None
    aligned = -(-(earliest - 1) // step) * step + 1  # ceil to the next k*step+1
    last = turn_tokens[-1][0]
    return aligned if aligned <= last else earliest


def recent_history(
    thread_id: str, *, budget: int, step: int, chars_per_token: float, max_scan: int = 200
) -> list[ModelMessage]:
    """Recent turns of this thread as PydanticAI history, bounded by `budget` tokens.

    Whole turns only, so a tool call is never separated from its return.
    """
    with session() as s:
        last = s.exec(select(func.max(Message.turn)).where(Message.thread_id == thread_id)).one()
        if not last:
            return []
        rows = s.exec(
            select(Message)
            .where(Message.thread_id == thread_id, Message.turn > last - max_scan)
            .order_by(col(Message.turn), col(Message.id))
        ).all()

    by_turn: dict[int, list[Message]] = {}
    for r in rows:
        by_turn.setdefault(r.turn, []).append(r)
    params = ModelRequestParameters()
    turns = {t: _turn_messages(rs) for t, rs in by_turn.items()}
    sizes = [(t, estimate_tokens(m, params, chars_per_token)) for t, m in turns.items()]

    start = choose_window_start(sizes, budget, step)
    if start is None:
        return []
    return [m for t, msgs in turns.items() if t >= start for m in msgs]


def thread_messages(thread_id: str) -> list[Message]:
    with session() as s:
        return list(
            s.exec(
                select(Message)
                .where(Message.thread_id == thread_id)
                .order_by(col(Message.turn), col(Message.id))
            ).all()
        )
