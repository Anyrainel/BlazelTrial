"""Memory store handlers (docs/MEMORY_CONTRACTS.md): ownership-enforced, bounded, versioned.

Callers are built from trusted run dependencies only; nothing here accepts an owner id from a
model. Anything the caller may not see is reported exactly like something that doesn't exist.
Mutations are serialized by one process-wide lock and run in one SQLite transaction together
with their index rows.
"""

import re
import threading
from dataclasses import dataclass, field
from datetime import datetime

from sqlmodel import Session, col, select

from chatbot.config import get_settings
from chatbot.memory import refs, search
from chatbot.memory.db import session
from chatbot.memory.models import (
    Checkpoint,
    DocPassage,
    DocRef,
    EpisodeNote,
    MemoryRecord,
    Message,
    NoteSource,
    RecordKind,
    RecordSource,
    Role,
    Store,
    ToolOutput,
    utcnow,
)

MAX_TEXT = 500
MAX_KEY = 64
MAX_SOURCES = 10
MAX_READ_CHARS = 2000
MAX_EXCERPT = 300
MAX_K = 10
MAX_LIST = 50  # current records one listing may return (a survey of topics, not search hits)
MAX_EXPAND = 5
MAX_VERSIONS = 20
MAX_NOTE = 1500
MAX_SAVED_READ = 6000  # chars of a saved tool output returned by one read
MAX_RESULT_CHARS = 2400  # total excerpt text one search returns

_write_lock = threading.Lock()


class MemoryAccessError(ValueError):
    """A request the model can correct: bad input, unknown/invisible ref, stale version."""


@dataclass(frozen=True)
class MemoryCaller:
    """Who is asking. Built from trusted run dependencies (ChatDeps), never from tool args."""

    user_id: str
    session_id: str


@dataclass(frozen=True)
class SourceView:
    ref: str
    excerpt: str


@dataclass(frozen=True)
class RecordView:
    ref: str
    store: str
    kind: str
    key: str
    text: str
    created_at: datetime
    current: bool
    supersedes: str | None
    superseded_by: str | None
    sources: list[SourceView] = field(default_factory=list)


@dataclass(frozen=True)
class MessageView:
    ref: str
    role: str
    turn: int
    text: str
    created_at: datetime


@dataclass(frozen=True)
class PassageView:
    ref: str
    doc_id: str
    section: str
    text: str


@dataclass(frozen=True)
class NoteView:
    ref: str
    text: str
    covers: list[str]  # exact msg refs the note was written from, oldest first
    created_at: datetime


@dataclass(frozen=True)
class SavedView:
    """A saved tool output (`out:`) or run checkpoint (`ckpt:`)."""

    ref: str
    label: str
    text: str
    created_at: datetime


@dataclass(frozen=True)
class Hit:
    ref: str
    type: str  # "record" | "message" | "note" | "passage"
    store: str
    excerpt: str
    current: bool
    created_at: datetime
    found_by: tuple[str, ...]  # candidate lists that returned it: bm25 / notes / vectors
    turn: int | None = None  # message hits
    covers: str | None = None  # note hits: "msg:a..msg:b (n messages)"
    role: str | None = None  # message hits: "user" | "assistant"


def today() -> str:
    """Today's local date (ISO). Prompts state it so relative dates can be made absolute."""
    return datetime.now().astimezone().date().isoformat()


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _not_found(ref: str) -> MemoryAccessError:
    return MemoryAccessError(f"unknown or not visible: {ref}")


# --- visibility -------------------------------------------------------------------------------


def _visible_record(s: Session, caller: MemoryCaller, rid: int) -> MemoryRecord | None:
    r = s.get(MemoryRecord, rid)
    if r is None or r.user_id != caller.user_id:
        return None
    if r.store == Store.SESSION and r.session_id != caller.session_id:
        return None
    return r


def _visible_message(s: Session, caller: MemoryCaller, mid: int) -> Message | None:
    m = s.get(Message, mid)
    if m is None or m.forgotten or m.user_id != caller.user_id or m.thread_id != caller.session_id:
        return None
    return m


def _visible_note(s: Session, caller: MemoryCaller, nid: int) -> EpisodeNote | None:
    n = s.get(EpisodeNote, nid)
    if n is None or n.user_id != caller.user_id or n.session_id != caller.session_id:
        return None
    return n


def _note_covers(s: Session, note_id: int) -> list[int]:
    return list(
        s.exec(
            select(NoteSource.message_id)
            .where(NoteSource.note_id == note_id)
            .order_by(col(NoteSource.message_id))
        ).all()
    )


def _visible_saved(s: Session, caller: MemoryCaller, model, id_: int):
    row = s.get(model, id_)
    if row is None or row.user_id != caller.user_id or row.session_id != caller.session_id:
        return None
    return row


def _visible_passage(s: Session, caller: MemoryCaller, pid: int) -> DocPassage | None:
    p = s.get(DocPassage, pid)
    if p is None:
        return None
    opened = s.exec(
        select(DocRef.id).where(DocRef.thread_id == caller.session_id, DocRef.doc_id == p.doc_id)
    ).first()
    return p if opened else None


def _resolve_excerpt(s: Session, caller: MemoryCaller, ref: str) -> str:
    """Excerpt of a visible source; raises not-found otherwise."""
    parsed = refs.parse(ref)
    if parsed is None:
        raise _not_found(ref)
    if parsed.type == "msg" and (m := _visible_message(s, caller, parsed.id)):
        return _clip(m.text, MAX_EXCERPT)
    if parsed.type == "psg" and (p := _visible_passage(s, caller, parsed.id)):
        return _clip(p.text, MAX_EXCERPT)
    if parsed.type == "rec" and (r := _visible_record(s, caller, parsed.id)):
        return _clip(r.text, MAX_EXCERPT)
    if parsed.type == "note" and (n := _visible_note(s, caller, parsed.id)):
        return _clip(n.text, MAX_EXCERPT)
    if parsed.type == "out" and (o := _visible_saved(s, caller, ToolOutput, parsed.id)):
        return _clip(o.content, MAX_EXCERPT)
    if parsed.type == "ckpt" and (c := _visible_saved(s, caller, Checkpoint, parsed.id)):
        return _clip(c.text, MAX_EXCERPT)
    raise _not_found(ref)


# --- views ------------------------------------------------------------------------------------


def _view(s: Session, r: MemoryRecord, *, with_sources: bool = True) -> RecordView:
    sources = []
    if with_sources:
        rows = s.exec(
            select(RecordSource)
            .where(RecordSource.record_id == r.id)
            .order_by(col(RecordSource.id))
        ).all()
        sources = [SourceView(row.ref, row.excerpt) for row in rows]
    return RecordView(
        ref=refs.rec(r.id),
        store=str(r.store.value),
        kind=str(r.kind.value),
        key=r.key,
        text=_clip(r.text, MAX_READ_CHARS),
        created_at=r.created_at,
        current=r.superseded_at is None,
        supersedes=refs.rec(r.supersedes_id) if r.supersedes_id else None,
        superseded_by=refs.rec(r.superseded_by) if r.superseded_by else None,
        sources=sources,
    )


# --- writes -----------------------------------------------------------------------------------


def normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", key.strip().lower()).strip("_")


def write(
    caller: MemoryCaller,
    *,
    store: str,
    kind: str,
    key: str,
    text: str,
    sources: list[str],
    supersedes: str | None = None,
    idempotency_key: str | None = None,
) -> RecordView:
    """Store a new record, or a new version of one (`supersedes` = its current version)."""
    try:
        store_ = Store(store)
        kind_ = RecordKind(kind)
    except ValueError as exc:
        raise MemoryAccessError(f"invalid store or kind: {exc}") from None
    key_ = normalize_key(key)
    text = text.strip()
    if not key_ or len(key_) > MAX_KEY:
        raise MemoryAccessError(f"key must be 1-{MAX_KEY} characters of letters/digits")
    if not text or len(text) > MAX_TEXT:
        raise MemoryAccessError(f"text must be 1-{MAX_TEXT} characters (got {len(text)})")
    unique_sources = list(dict.fromkeys(src.strip() for src in sources))
    if not 1 <= len(unique_sources) <= MAX_SOURCES:
        raise MemoryAccessError(f"cite 1-{MAX_SOURCES} sources")
    parsed = [refs.parse(src) for src in unique_sources]
    if not any(p is not None and p.type in refs.EVIDENCE for p in parsed):
        raise MemoryAccessError("cite at least one original source (msg: or psg:)")

    with _write_lock, session() as s:
        if idempotency_key:
            done = s.exec(
                select(MemoryRecord).where(MemoryRecord.idempotency_key == idempotency_key)
            ).first()
            if done is not None:
                if _visible_record(s, caller, done.id) is None:
                    raise MemoryAccessError("idempotency key already used")
                return _view(s, done)

        excerpts = [_resolve_excerpt(s, caller, src) for src in unique_sources]

        previous = None
        if supersedes is not None:
            sp = refs.parse(supersedes)
            if sp is None or sp.type != "rec":
                raise _not_found(supersedes)
            previous = _visible_record(s, caller, sp.id)
            if previous is None:
                raise _not_found(supersedes)
            if previous.superseded_by is not None or previous.superseded_at is not None:
                raise MemoryAccessError(
                    f"{supersedes} is not the current version "
                    f"(current: {refs.rec(_current_id(s, previous))})"
                )
            if previous.store != store_ or previous.kind != kind_:
                raise MemoryAccessError(
                    f"{supersedes} is a {previous.store.value} {previous.kind.value}; "
                    "a new version must keep its store and kind"
                )

        if kind_ == RecordKind.PREFERENCE:
            _check_preference_budget(s, caller, store_, text, replacing=previous)

        record = MemoryRecord(
            user_id=caller.user_id,
            store=store_,
            session_id=caller.session_id if store_ == Store.SESSION else None,
            origin_session_id=caller.session_id,
            kind=kind_,
            key=key_,
            text=text,
            supersedes_id=previous.id if previous else None,
            idempotency_key=idempotency_key,
        )
        s.add(record)
        s.flush()  # assigns record.id inside the transaction
        for src, excerpt in zip(unique_sources, excerpts, strict=True):
            s.add(RecordSource(record_id=record.id, ref=src, excerpt=excerpt))
        if previous is not None:
            previous.superseded_at = utcnow()
            previous.superseded_by = record.id
            s.add(previous)
        search.index_record(s.connection(), record)  # same transaction as the record
        s.commit()
        return _view(s, record)


def preference_limit(store: Store) -> int:
    settings = get_settings()
    if store == Store.SESSION:
        return settings.session_preferences_max_chars
    return settings.preferences_max_chars


def _current_preferences(
    s: Session, caller: MemoryCaller, store: Store | None = None
) -> list[MemoryRecord]:
    """Current preference records the caller may see, oldest first (stable prompt order)."""
    rows = s.exec(
        select(MemoryRecord)
        .where(
            MemoryRecord.user_id == caller.user_id,
            MemoryRecord.kind == RecordKind.PREFERENCE,
            col(MemoryRecord.superseded_at).is_(None),
        )
        .order_by(col(MemoryRecord.id))
    ).all()
    return [
        r
        for r in rows
        if (store is None or r.store == store)
        and (r.store == Store.LONG_TERM or r.session_id == caller.session_id)
    ]


def _check_preference_budget(
    s: Session,
    caller: MemoryCaller,
    store: Store,
    text: str,
    replacing: MemoryRecord | None,
) -> None:
    """Preferences are injected into every request, so their total size is capped."""
    others = [
        r for r in _current_preferences(s, caller, store) if not replacing or r.id != replacing.id
    ]
    used, limit = sum(len(r.text) for r in others), preference_limit(store)
    if used + len(text) > limit:
        raise MemoryAccessError(
            f"preference limit: {used} of {limit} characters are in use for {store.value} "
            f"preferences and this one needs {len(text)}; make it shorter, or merge it with "
            "another, or retire one that no longer matters"
        )


def retire(caller: MemoryCaller, ref: str) -> RecordView:
    """Stop using a current record without replacing it (its history stays)."""
    parsed = refs.parse(ref)
    with _write_lock, session() as s:
        r = _visible_record(s, caller, parsed.id) if parsed and parsed.type == "rec" else None
        if r is None:
            raise _not_found(ref)
        if r.superseded_at is not None:
            raise MemoryAccessError(f"{ref} is not a current record")
        r.superseded_at = utcnow()
        s.add(r)
        s.commit()
        return _view(s, r, with_sources=False)


def current_preferences(caller: MemoryCaller) -> list[RecordView]:
    """Long-term preferences, then this conversation's, within their caps (oldest dropped first
    if a cap was lowered after they were written)."""
    out: list[RecordView] = []
    with session() as s:
        for store in (Store.LONG_TERM, Store.SESSION):
            rows = _current_preferences(s, caller, store)
            while rows and sum(len(r.text) for r in rows) > preference_limit(store):
                rows = rows[1:]
            out += [_view(s, r, with_sources=False) for r in rows]
    return out


def _current_id(s: Session, r: MemoryRecord) -> int:
    seen = 0
    while r.superseded_by is not None and seen < 1000:
        r = s.get(MemoryRecord, r.superseded_by)
        seen += 1
    return r.id


# --- reads ------------------------------------------------------------------------------------


def read(caller: MemoryCaller, ref: str) -> RecordView | MessageView | PassageView:
    parsed = refs.parse(ref)
    if parsed is None:
        raise _not_found(ref)
    with session() as s:
        if parsed.type == "rec" and (r := _visible_record(s, caller, parsed.id)):
            return _view(s, r)
        if parsed.type == "msg" and (m := _visible_message(s, caller, parsed.id)):
            return MessageView(
                ref, str(m.role.value), m.turn, _clip(m.text, MAX_READ_CHARS), m.created_at
            )
        if parsed.type == "psg" and (p := _visible_passage(s, caller, parsed.id)):
            return PassageView(ref, p.doc_id, p.section, _clip(p.text, MAX_READ_CHARS))
        if parsed.type == "note" and (n := _visible_note(s, caller, parsed.id)):
            covers = [refs.msg(mid) for mid in _note_covers(s, n.id)]
            return NoteView(ref, _clip(n.text, MAX_READ_CHARS), covers, n.created_at)
        if parsed.type == "out" and (o := _visible_saved(s, caller, ToolOutput, parsed.id)):
            return SavedView(
                ref, f"saved {o.tool_name} result", o.content[:MAX_SAVED_READ], o.created_at
            )
        if parsed.type == "ckpt" and (c := _visible_saved(s, caller, Checkpoint, parsed.id)):
            return SavedView(ref, "run checkpoint", c.text[:MAX_SAVED_READ], c.updated_at)
    raise _not_found(ref)


def expand(caller: MemoryCaller, ref: str, *, before: int = 2, after: int = 2) -> list[MessageView]:
    """Messages around `ref` in the same session (the message itself included)."""
    parsed = refs.parse(ref)
    before, after = max(0, min(before, MAX_EXPAND)), max(0, min(after, MAX_EXPAND))
    with session() as s:
        center = _visible_message(s, caller, parsed.id) if parsed and parsed.type == "msg" else None
        if center is None:
            raise _not_found(ref)
        same = (Message.thread_id == caller.session_id) & (Message.forgotten == False)  # noqa: E712
        earlier = s.exec(
            select(Message)
            .where(same, col(Message.id) < center.id)
            .order_by(col(Message.id).desc())
            .limit(before)
        ).all()
        later = s.exec(
            select(Message)
            .where(same, col(Message.id) > center.id)
            .order_by(col(Message.id))
            .limit(after)
        ).all()
        rows = [*reversed(earlier), center, *later]
        return [
            MessageView(
                refs.msg(m.id),
                str(m.role.value),
                m.turn,
                _clip(m.text, MAX_READ_CHARS),
                m.created_at,
            )
            for m in rows
        ]


def versions(caller: MemoryCaller, ref: str) -> list[RecordView]:
    """The record's version chain, oldest first (at most MAX_VERSIONS, newest kept)."""
    parsed = refs.parse(ref)
    with session() as s:
        r = _visible_record(s, caller, parsed.id) if parsed and parsed.type == "rec" else None
        if r is None:
            raise _not_found(ref)
        chain = [r]
        while chain[0].supersedes_id and len(chain) < 1000:
            chain.insert(0, s.get(MemoryRecord, chain[0].supersedes_id))
        while chain[-1].superseded_by and len(chain) < 1000:
            chain.append(s.get(MemoryRecord, chain[-1].superseded_by))
        return [_view(s, x, with_sources=False) for x in chain[-MAX_VERSIONS:]]


def list_current(
    caller: MemoryCaller, *, store: str = "both", kind: str | None = None, k: int = 10
) -> list[RecordView]:
    """The newest current records the caller may see (at most MAX_LIST), to survey topics."""
    if store not in ("session", "long_term", "both"):
        raise MemoryAccessError("store must be session, long_term or both")
    stores = {Store.SESSION, Store.LONG_TERM} if store == "both" else {Store(store)}
    with session() as s:
        query = select(MemoryRecord).where(
            MemoryRecord.user_id == caller.user_id, col(MemoryRecord.superseded_at).is_(None)
        )
        if kind:
            try:
                query = query.where(MemoryRecord.kind == RecordKind(kind))
            except ValueError:
                raise MemoryAccessError("kind must be fact, preference or task") from None
        rows = s.exec(query.order_by(col(MemoryRecord.id).desc())).all()
        visible = [
            r
            for r in rows
            if r.store in stores
            and (r.store == Store.LONG_TERM or r.session_id == caller.session_id)
        ]
        return [_view(s, r, with_sources=False) for r in visible[: max(1, min(k, MAX_LIST))]]


def current_with_key(caller: MemoryCaller, *, store: str, kind: str, key: str) -> RecordView | None:
    """The current record of this store and kind under `key`, however many records exist.

    A query by key (not a listing), so a duplicate-save guard cannot be fooled by a long list."""
    try:
        store_, kind_ = Store(store), RecordKind(kind)
    except ValueError:
        raise MemoryAccessError("invalid store or kind") from None
    with session() as s:
        rows = s.exec(
            select(MemoryRecord)
            .where(
                MemoryRecord.user_id == caller.user_id,
                MemoryRecord.store == store_,
                MemoryRecord.kind == kind_,
                MemoryRecord.key == normalize_key(key),
                col(MemoryRecord.superseded_at).is_(None),
            )
            .order_by(col(MemoryRecord.id).desc())
        ).all()
        for r in rows:
            if store_ == Store.LONG_TERM or r.session_id == caller.session_id:
                return _view(s, r, with_sources=False)
    return None


# --- saved tool outputs and checkpoints (written by the context fitter) -------------------------


def save_tool_output(caller: MemoryCaller, tool_call_id: str, tool_name: str, content: str) -> str:
    """Persist a completed tool result; idempotent per tool call. Returns its `out:` ref."""
    with _write_lock, session() as s:
        row = s.exec(select(ToolOutput).where(ToolOutput.tool_call_id == tool_call_id)).first()
        if row is None:
            row = ToolOutput(
                user_id=caller.user_id,
                session_id=caller.session_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                content=content,
            )
            s.add(row)
            s.commit()
        return f"out:{row.id}"


def save_checkpoint(caller: MemoryCaller, run_id: str, text: str) -> str:
    """Create or refresh the checkpoint of one run. Returns its `ckpt:` ref."""
    with _write_lock, session() as s:
        row = s.exec(select(Checkpoint).where(Checkpoint.run_id == run_id)).first()
        if row is None:
            row = Checkpoint(
                user_id=caller.user_id, session_id=caller.session_id, run_id=run_id, text=text
            )
        else:
            row.text, row.updated_at = text, utcnow()
        s.add(row)
        s.commit()
        return f"ckpt:{row.id}"


# --- notes (written by maintenance, not by the model) -------------------------------------------


def add_note(user_id: str, session_id: str, message_ids: list[int], text: str) -> str:
    """Store an episode note over one contiguous, not-yet-covered segment of a session.

    Trusted maintenance API (the model never calls it). Validates that the messages exist,
    belong to the session, form a contiguous run, and overlap no existing note.
    """
    text = text.strip()
    if not text or len(text) > MAX_NOTE:
        raise MemoryAccessError(f"note text must be 1-{MAX_NOTE} characters")
    ids = sorted(set(message_ids))
    if not ids:
        raise MemoryAccessError("a note must cover at least one message")
    with _write_lock, session() as s:
        in_range = s.exec(
            select(Message.id).where(
                Message.thread_id == session_id,
                Message.user_id == user_id,
                col(Message.id) >= ids[0],
                col(Message.id) <= ids[-1],
            )
        ).all()
        if sorted(in_range) != ids:
            raise MemoryAccessError("note must cover a contiguous run of this session's messages")
        taken = s.exec(
            select(NoteSource.message_id).where(col(NoteSource.message_id).in_(ids))
        ).first()
        if taken is not None:
            raise MemoryAccessError(f"{refs.msg(taken)} is already covered by another note")
        note = EpisodeNote(
            user_id=user_id,
            session_id=session_id,
            start_message_id=ids[0],
            end_message_id=ids[-1],
            text=text,
        )
        s.add(note)
        s.flush()
        s.add_all(NoteSource(note_id=note.id, message_id=mid) for mid in ids)
        search.index_note(s.connection(), note)
        s.commit()
        return refs.note(note.id)


# --- search -------------------------------------------------------------------------------------

SOURCES = ("records", "messages", "notes", "passages", "vectors")
DEFAULT_SOURCES = ("records", "messages", "notes", "passages")


def search_memory(
    caller: MemoryCaller,
    query: str,
    *,
    store: str = "both",
    k: int = 5,
    sources: tuple[str, ...] = DEFAULT_SOURCES,
    newest_first: bool = False,
) -> list[Hit]:
    """Fused BM25 search over memory records, raw session messages and episode notes, plus
    optional vector candidates for messages.

    Every candidate list is filtered by owner/store inside SQL before ranking. Raw messages are
    always searched on their own list, so a detail a note omitted (or got wrong) is still
    found. Message hits are deduplicated per turn; note hits say which messages they cover.
    The total excerpt text is capped at MAX_RESULT_CHARS. Relevance picks the hits;
    `newest_first` only orders those hits by date (ties keep relevance order), so a later
    correction is read before the statement it replaced.
    """
    if store not in ("session", "long_term", "both"):
        raise MemoryAccessError("store must be session, long_term or both")
    unknown = set(sources) - set(SOURCES)
    if unknown:
        raise MemoryAccessError(f"unknown sources: {sorted(unknown)}")
    k = max(1, min(k, MAX_K))
    pool = k * 3
    stores = {"session", "long_term"} if store == "both" else {store}
    in_session = "session" in stores
    settings = get_settings()

    lists: dict[str, list[tuple[str, int]]] = {}
    if "records" in sources:
        ids = search.search_records(caller.user_id, caller.session_id, stores, query, k=pool)
        lists["bm25:records"] = [("record", i) for i in ids]
    if "messages" in sources and in_session:
        ids = search.search_session_messages(caller.session_id, query, k=pool)
        lists["bm25:messages"] = [("message", i) for i in ids]
    if "notes" in sources and in_session:
        ids = search.search_notes(caller.user_id, caller.session_id, query, k=pool)
        lists["bm25:notes"] = [("note", i) for i in ids]
    if "passages" in sources and in_session:
        with session() as s:
            doc_ids = list(
                s.exec(select(DocRef.doc_id).where(DocRef.thread_id == caller.session_id)).all()
            )
        ids = search.search_passages_bm25(doc_ids, query, k=pool)
        lists["bm25:passages"] = [("passage", i) for i in ids]
    if "vectors" in sources and in_session:
        ids = search.vector_session_messages(
            caller.session_id, query, k=pool, max_distance=settings.retrieval_max_distance
        )
        lists["vectors:messages"] = [("message", i) for i in ids]

    scores: dict[tuple[str, int], float] = {}
    found_by: dict[tuple[str, int], list[str]] = {}
    for name, items in lists.items():
        for rank, item in enumerate(items):
            scores[item] = scores.get(item, 0.0) + 1.0 / (60 + rank + 1)
            found_by.setdefault(item, []).append(name)

    hits: list[Hit] = []
    seen_turns: set[int] = set()
    used = 0
    with session() as s:
        for kind, id_ in sorted(scores, key=lambda key: -scores[key]):
            if len(hits) >= k:
                break
            via = tuple(found_by[(kind, id_)])
            hit = None
            if kind == "record" and (r := _visible_record(s, caller, id_)):
                hit = Hit(
                    refs.rec(r.id),
                    "record",
                    str(r.store.value),
                    _clip(r.text, MAX_EXCERPT),
                    r.superseded_at is None,
                    r.created_at,
                    via,
                )
            elif kind == "message" and (m := _visible_message(s, caller, id_)):
                if m.turn in seen_turns:
                    continue  # one hit per turn: the best-ranked side of it
                seen_turns.add(m.turn)
                who = "user" if m.role == Role.USER else "assistant"
                hit = Hit(
                    refs.msg(m.id),
                    "message",
                    "session",
                    f"{who}: {_clip(m.text, MAX_EXCERPT)}",
                    True,
                    m.created_at,
                    via,
                    turn=m.turn,
                    role=who,
                )
            elif kind == "passage" and (p := _visible_passage(s, caller, id_)):
                hit = Hit(
                    f"psg:{p.id}",
                    "passage",
                    "session",
                    f"[{p.doc_id} § {p.section}] {_clip(p.text, MAX_EXCERPT)}",
                    True,
                    utcnow(),
                    via,
                )
            elif kind == "note" and (n := _visible_note(s, caller, id_)):
                covered = _note_covers(s, n.id)
                span = f"{refs.msg(covered[0])}..{refs.msg(covered[-1])}"
                hit = Hit(
                    refs.note(n.id),
                    "note",
                    "session",
                    _clip(n.text, MAX_EXCERPT),
                    True,
                    n.created_at,
                    via,
                    covers=f"{span} ({len(covered)} messages)",
                )
            if hit is None:
                continue
            if used + len(hit.excerpt) > MAX_RESULT_CHARS:
                break
            used += len(hit.excerpt)
            hits.append(hit)
    if newest_first:
        # stable: relevance breaks ties; passages carry no date, so they go last
        hits.sort(key=lambda h: (h.type != "passage", h.created_at), reverse=True)
    return hits
