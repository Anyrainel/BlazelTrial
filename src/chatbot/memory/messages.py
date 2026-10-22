"""Message store: persist turns and rebuild a bounded, cache-friendly history window."""

from dataclasses import dataclass, replace
from datetime import datetime

from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters
from sqlmodel import col, func, select

from chatbot.agent.budget import estimate_tokens
from chatbot.memory.db import session
from chatbot.memory.models import Message, Role, Source, Thread, ThreadKind, utcnow

SLACK_THREAD_ID = "slack-dm"


def ensure_thread(user_id: str, thread_id: str) -> Thread:
    with session() as s:
        thread = s.get(Thread, thread_id)
        if thread is None:
            kind = ThreadKind.SLACK if thread_id.startswith("slack-") else ThreadKind.SESSION
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
    at: datetime | None = None,
) -> list[Message]:
    """Store one turn atomically (`reply_text=None`: user message only). Returns stored rows."""
    with session() as s:
        last = s.exec(select(func.max(Message.turn)).where(Message.thread_id == thread_id)).one()
        turn = (last or 0) + 1
        rows = [
            Message(
                thread_id=thread_id,
                user_id=user_id,
                turn=turn,
                role=Role.USER,
                text=user_text,
                source=source,
                created_at=at or utcnow(),
            )
        ]
        if reply_text is not None:
            rows.append(
                Message(
                    thread_id=thread_id,
                    user_id=user_id,
                    turn=turn,
                    role=Role.ASSISTANT,
                    text=reply_text,
                    source=source,
                    run_messages_json=run_messages_json,
                    created_at=at or utcnow(),
                )
            )
        s.add_all(rows)
        s.commit()
        return rows


def add_reply(user_message: Message, reply_text: str, *, run_messages_json: str | None) -> Message:
    """Store the assistant reply for a user message stored earlier in the same turn."""
    with session() as s:
        row = Message(
            thread_id=user_message.thread_id,
            user_id=user_message.user_id,
            turn=user_message.turn,
            role=Role.ASSISTANT,
            text=reply_text,
            source=user_message.source,
            run_messages_json=run_messages_json,
        )
        s.add(row)
        s.commit()
        return row


_REPLAY_TOOL_RETURN_CHARS = 400


def _shrink_tool_returns(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Cut long tool returns in replayed turns to a fixed stub. Deterministic, so a turn replays
    identically every time (stable, cacheable prefix); the tool can be called again if needed."""
    out: list[ModelMessage] = []
    for m in messages:
        if isinstance(m, ModelRequest) and any(isinstance(p, ToolReturnPart) for p in m.parts):
            parts = []
            for p in m.parts:
                if isinstance(p, ToolReturnPart):
                    text = p.model_response_str()
                    if len(text) > _REPLAY_TOOL_RETURN_CHARS:
                        p = replace(
                            p,
                            content=text[:_REPLAY_TOOL_RETURN_CHARS]
                            + " … [older tool output truncated; call the tool again for details]",
                        )
                parts.append(p)
            m = replace(m, parts=parts)
        out.append(m)
    return out


FORGOTTEN_USER = "[Forgotten content.]"
FORGOTTEN_REPLY = "[This turn was completed. Its content was forgotten.]"


def _turn_messages(rows: list[Message]) -> list[ModelMessage]:
    """PydanticAI messages for one turn: the stored run if we have it, else plain text.
    A forgotten turn is replayed as a neutral completed pair, never with its content."""
    if any(r.forgotten for r in rows):
        return [
            ModelRequest(parts=[UserPromptPart(content=FORGOTTEN_USER)]),
            ModelResponse(parts=[TextPart(content=FORGOTTEN_REPLY)]),
        ]
    reply = next((r for r in rows if r.role == Role.ASSISTANT), None)
    if reply is not None and reply.run_messages_json:
        return _shrink_tool_returns(ModelMessagesTypeAdapter.validate_json(reply.run_messages_json))
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


@dataclass(frozen=True)
class HistoryWindow:
    messages: list[ModelMessage]
    start_turn: int  # first turn replayed verbatim; older turns need summaries/search


def recent_history(
    thread_id: str,
    *,
    budget: int,
    step: int,
    chars_per_token: float,
    max_scan: int = 200,
    before_turn: int | None = None,
) -> HistoryWindow:
    """Recent turns of this thread as PydanticAI history, bounded by `budget` tokens.

    Whole turns only, so a tool call is never separated from its return.
    """
    with session() as s:
        query = select(func.max(Message.turn)).where(Message.thread_id == thread_id)
        if before_turn is not None:  # exclude the turn being answered (already stored)
            query = query.where(Message.turn < before_turn)
        last = s.exec(query).one()
        if not last:
            return HistoryWindow([], 1)
        rows = s.exec(
            select(Message)
            .where(
                Message.thread_id == thread_id,
                Message.turn > last - max_scan,
                Message.turn <= last,
            )
            .order_by(col(Message.turn), col(Message.id))
        ).all()

    by_turn: dict[int, list[Message]] = {}
    for r in rows:
        by_turn.setdefault(r.turn, []).append(r)
    params = ModelRequestParameters()
    replay = {t: _turn_messages(rs) for t, rs in by_turn.items()}
    sizes = [(t, estimate_tokens(m, params, chars_per_token)) for t, m in replay.items()]

    start = choose_window_start(sizes, budget, step)
    if start is None:
        return HistoryWindow([], last + 1)
    return HistoryWindow([m for t, msgs in replay.items() if t >= start for m in msgs], start)


def turns(thread_id: str, turn_numbers: list[int]) -> dict[int, list[Message]]:
    """Messages of the given turns, grouped by turn (user first)."""
    with session() as s:
        rows = s.exec(
            select(Message)
            .where(Message.thread_id == thread_id, col(Message.turn).in_(turn_numbers))
            .order_by(col(Message.turn), col(Message.id))
        ).all()
    out: dict[int, list[Message]] = {}
    for r in rows:
        out.setdefault(r.turn, []).append(r)
    return out


def thread_messages(thread_id: str) -> list[Message]:
    with session() as s:
        return list(
            s.exec(
                select(Message)
                .where(Message.thread_id == thread_id)
                .order_by(col(Message.turn), col(Message.id))
            ).all()
        )
