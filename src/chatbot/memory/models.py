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


class FactKind(StrEnum):
    PREFERENCE = "preference"  # how the assistant should behave/write: always in context
    FACT = "fact"  # about the user's world: in context when relevant to the message


class Fact(SQLModel, table=True):
    """A remembered fact or standing instruction. Never edited: changes supersede it."""

    id: int | None = Field(default=None, primary_key=True)
    user_id: str = Field(index=True)
    scope: Scope = Field(index=True)
    thread_id: str | None = Field(default=None, index=True)  # set for thread scope only
    key: str = Field(index=True)  # short topic slug, e.g. "manager", "spelling"
    kind: FactKind = Field(default=FactKind.FACT, index=True)
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


class DocNote(SQLModel, table=True):
    """Distilled notes of one document version (map-reduce over chunks), computed once."""

    id: int | None = Field(default=None, primary_key=True)
    doc_id: str = Field(index=True)
    sha256: str = Field(index=True)
    text: str
    chunk_count: int
    created_at: datetime = Field(default_factory=utcnow)


class DocPassage(SQLModel, table=True):
    """A ~1k-char passage of a document; indexed for `search_doc`."""

    id: int | None = Field(default=None, primary_key=True)
    doc_id: str = Field(index=True)
    sha256: str = Field(index=True)
    idx: int
    section: str
    text: str


class DocRef(SQLModel, table=True):
    """A document opened in a thread, so "that plan" can be resolved later."""

    id: int | None = Field(default=None, primary_key=True)
    thread_id: str = Field(index=True)
    doc_id: str
    last_used_at: datetime = Field(default_factory=utcnow)


class OutboundMessage(SQLModel, table=True):
    """A `send_message` call. Nothing is really sent; this log is the effect."""

    id: int | None = Field(default=None, primary_key=True)
    thread_id: str = Field(index=True)
    recipient: str
    text: str
    created_at: datetime = Field(default_factory=utcnow)


class Store(StrEnum):
    SESSION = "session"
    LONG_TERM = "long_term"


class RecordKind(StrEnum):
    FACT = "fact"
    PREFERENCE = "preference"
    TASK = "task"


class MemoryRecord(SQLModel, table=True):
    """One version of an agent-written memory record (docs/MEMORY_CONTRACTS.md §4).

    Never edited in place except to mark it superseded."""

    id: int | None = Field(default=None, primary_key=True)
    user_id: str = Field(index=True)
    store: Store = Field(index=True)
    session_id: str | None = Field(default=None, index=True)  # set for session-store records
    origin_session_id: str | None = None  # where it was written (provenance only)
    kind: RecordKind
    key: str = Field(index=True)
    text: str
    created_at: datetime = Field(default_factory=utcnow)
    supersedes_id: int | None = Field(default=None, index=True)
    superseded_at: datetime | None = Field(default=None, index=True)
    superseded_by: int | None = None
    idempotency_key: str | None = Field(default=None, unique=True)
    legacy_fact_id: int | None = Field(default=None, unique=True)  # set by Fact migration


class RecordSource(SQLModel, table=True):
    """A source a record cites. `excerpt` is the snapshot long-term readers in other sessions
    may see (the rest of the source conversation stays invisible to them)."""

    id: int | None = Field(default=None, primary_key=True)
    record_id: int = Field(foreign_key="memoryrecord.id", index=True)
    ref: str = Field(index=True)
    excerpt: str


class EpisodeNote(SQLModel, table=True):
    """A derived, searchable note over one fixed, non-overlapping segment of a session's
    messages (docs/MEMORY_CONTRACTS.md §4). Navigation aid, never evidence by itself."""

    id: int | None = Field(default=None, primary_key=True)
    user_id: str = Field(index=True)
    session_id: str = Field(index=True)
    start_message_id: int = Field(index=True)
    end_message_id: int = Field(index=True)
    text: str
    created_at: datetime = Field(default_factory=utcnow)


class NoteSource(SQLModel, table=True):
    """Exact coverage: one row per message a note was written from."""

    id: int | None = Field(default=None, primary_key=True)
    note_id: int = Field(foreign_key="episodenote.id", index=True)
    message_id: int = Field(index=True)
