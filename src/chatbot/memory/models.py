"""SQLModel tables. Raw SQL (FTS5 / vector virtual tables) lives only in `memory/search.py`."""

from datetime import UTC, datetime
from enum import StrEnum

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(UTC)


class ThreadKind(StrEnum):
    SESSION = "session"
    SLACK = "slack"


class Role(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class Source(StrEnum):
    LIVE = "live"
    INGEST = "ingest"


class Thread(SQLModel, table=True):
    id: str = Field(primary_key=True)
    user_id: str = Field(index=True)
    kind: ThreadKind
    created_at: datetime = Field(default_factory=utcnow)


class Message(SQLModel, table=True):
    """One user or assistant message. A turn = a user message plus its assistant reply."""

    id: int | None = Field(default=None, primary_key=True)
    thread_id: str = Field(foreign_key="thread.id", index=True)
    user_id: str = Field(index=True)
    turn: int = Field(index=True)  # 1-based turn number within the thread
    role: Role
    text: str
    source: Source = Source.LIVE
    # Assistant rows only: PydanticAI messages of this turn (incl. tool calls/returns) as JSON,
    # replayed verbatim so history keeps tool call/return pairs intact.
    run_messages_json: str | None = None
    # User rows only: whether fact extraction has processed this message yet.
    extracted: bool = Field(default=False, index=True)
    created_at: datetime = Field(default_factory=utcnow, index=True)


class Scope(StrEnum):
    USER = "user"  # about the user / standing preferences: visible in every thread
    THREAD = "thread"  # only for the session it was said in


class Fact(SQLModel, table=True):
    """A remembered fact or standing instruction. Never edited: changes supersede it."""

    id: int | None = Field(default=None, primary_key=True)
    user_id: str = Field(index=True)
    scope: Scope = Field(index=True)
    thread_id: str | None = Field(default=None, index=True)  # set for thread scope only
    key: str = Field(index=True)  # short topic slug, e.g. "manager", "spelling"
    text: str
    source_thread_id: str
    source_turn: int
    created_at: datetime = Field(default_factory=utcnow)
    superseded_at: datetime | None = Field(default=None, index=True)
    superseded_by: int | None = None


class Summary(SQLModel, table=True):
    """Summary of a contiguous chunk of turns that scrolled out of the replayed window."""

    id: int | None = Field(default=None, primary_key=True)
    thread_id: str = Field(foreign_key="thread.id", index=True)
    start_turn: int
    end_turn: int = Field(index=True)
    text: str
    created_at: datetime = Field(default_factory=utcnow)
