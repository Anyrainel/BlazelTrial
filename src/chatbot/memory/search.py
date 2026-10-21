"""Hybrid search over past messages: FTS5 keyword ranking + sqlite-vec similarity, fused by
reciprocal rank. The only module with raw SQL (virtual tables SQLModel can't express)."""

import re
from dataclasses import dataclass
from functools import cache
from types import SimpleNamespace

import sqlite_vec
from sqlalchemy import Engine

from chatbot.memory.embeddings import get_embedder
from chatbot.memory.models import Message

_RRF_K = 60
_EMBED_CHARS = 2000
_STOPWORDS = frozenset(
    """a an and are as at be but by can did do does for from had has have how i if in is it its
    me my of on or our so that the their them then there these they this to was we were what
    when where which who why will with you your about after again all also any been before
    could just know like more most much not now only other over said same should some such than
    tell too very want would yes""".split()
)


@dataclass(frozen=True)
class Hit:
    message_id: int
    turn: int
    score: float


def create_fts(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5("
            "text, thread_id UNINDEXED, turn UNINDEXED, tokenize='porter unicode61')"
        )
        conn.exec_driver_sql(
            "CREATE VIRTUAL TABLE IF NOT EXISTS doc_fts USING fts5("
            "text, doc_id UNINDEXED, tokenize='porter unicode61')"
        )
        conn.exec_driver_sql(
            "CREATE VIRTUAL TABLE IF NOT EXISTS note_fts USING fts5("
            "text, user_id UNINDEXED, session_id UNINDEXED, tokenize='porter unicode61')"
        )
        conn.exec_driver_sql(
            "CREATE VIRTUAL TABLE IF NOT EXISTS record_fts USING fts5("
            "key, text, user_id UNINDEXED, store UNINDEXED, session_id UNINDEXED, "
            "tokenize='porter unicode61')"
        )


def _vec_table(dim: int) -> str:
    return f"message_vec_{dim}"


@cache
def _ensure_doc_vec(engine: Engine, dim: int) -> str:
    table = f"doc_vec_{dim}"
    with engine.begin() as conn:
        conn.exec_driver_sql(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING vec0("
            f"embedding float[{dim}] distance_metric=cosine, doc_id text)"
        )
    return table


@cache
def _ensure_vec(engine: Engine, dim: int) -> str:
    table = _vec_table(dim)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING vec0("
            f"embedding float[{dim}] distance_metric=cosine, thread_id text, turn integer)"
        )
    return table


def _engine() -> Engine:
    from chatbot.memory.db import get_engine

    return get_engine()


def index(messages: list[Message]) -> None:
    """Add messages to both indexes (rowid = message id)."""
    rows = [m for m in messages if m.id is not None and m.text.strip()]
    if not rows:
        return
    engine = _engine()
    embedder = get_embedder()
    table = _ensure_vec(engine, embedder.dim)
    vectors = embedder.embed([m.text[:_EMBED_CHARS] for m in rows])
    with engine.begin() as conn:
        for m, vec in zip(rows, vectors, strict=True):
            conn.exec_driver_sql(
                "INSERT INTO message_fts(rowid, text, thread_id, turn) VALUES (?, ?, ?, ?)",
                (m.id, m.text, m.thread_id, m.turn),
            )
            conn.exec_driver_sql(
                f"INSERT INTO {table}(rowid, embedding, thread_id, turn) VALUES (?, ?, ?, ?)",
                (m.id, sqlite_vec.serialize_float32(vec), m.thread_id, m.turn),
            )


def fts_query(text: str) -> str | None:
    """OR of the distinctive words in `text`, each quoted (no FTS syntax injection)."""
    words = []
    for w in re.findall(r"\w+", text.lower()):
        if len(w) >= 3 and w not in _STOPWORDS and w not in words:
            words.append(w)
    return " OR ".join(f'"{w}"' for w in words) or None


def search(
    thread_id: str,
    query: str,
    *,
    k: int,
    before_turn: int | None = None,
    max_distance: float = 1.0,
) -> list[Hit]:
    """Best-matching turns of `thread_id`, optionally only turns < `before_turn`.

    Keyword hits need a word match; vector hits need cosine distance <= `max_distance` (nearest
    neighbours always exist, relevant or not). Returns one hit per turn, best fused score first.
    """
    engine = _engine()
    pool = k * 4
    turn_filter = before_turn if before_turn is not None else 2**62
    ranked: dict[int, tuple[int, float]] = {}  # message_id -> (turn, rrf score)

    def add(rows) -> None:
        for rank, (mid, turn) in enumerate(rows):
            prev = ranked.get(mid, (turn, 0.0))[1]
            ranked[mid] = (turn, prev + 1.0 / (_RRF_K + rank + 1))

    with engine.begin() as conn:
        if q := fts_query(query):
            add(
                conn.exec_driver_sql(
                    "SELECT rowid, turn FROM message_fts WHERE message_fts MATCH ? "
                    "AND thread_id = ? AND turn < ? ORDER BY bm25(message_fts) LIMIT ?",
                    (q, thread_id, turn_filter, pool),
                ).all()
            )
        embedder = get_embedder()
        table = _ensure_vec(engine, embedder.dim)
        [vec] = embedder.embed([query[:_EMBED_CHARS]])
        near = conn.exec_driver_sql(
            f"SELECT rowid, turn, distance FROM {table} WHERE embedding MATCH ? AND k = ? "
            "AND thread_id = ? AND turn < ? ORDER BY distance",
            (sqlite_vec.serialize_float32(vec), pool, thread_id, turn_filter),
        ).all()
        add([(mid, turn) for mid, turn, dist in near if dist <= max_distance])

    best: dict[int, Hit] = {}
    for mid, (turn, score) in ranked.items():
        if turn not in best or score > best[turn].score:
            best[turn] = Hit(mid, turn, score)
    return sorted(best.values(), key=lambda h: h.score, reverse=True)[:k]


def delete(message_ids: list[int]) -> None:
    """Remove messages from both indexes (used by forget)."""
    if not message_ids:
        return
    engine = _engine()
    table = _ensure_vec(engine, get_embedder().dim)
    with engine.begin() as conn:
        for mid in message_ids:
            conn.exec_driver_sql("DELETE FROM message_fts WHERE rowid = ?", (mid,))
            conn.exec_driver_sql(f"DELETE FROM {table} WHERE rowid = ?", (mid,))


# --- document passages ---------------------------------------------------------------------


def index_passages(passages: list[tuple[int, str, str]]) -> None:
    """Index document passages given as (passage_id, doc_id, text)."""
    if not passages:
        return
    engine = _engine()
    embedder = get_embedder()
    table = _ensure_doc_vec(engine, embedder.dim)
    vectors = embedder.embed([text[:_EMBED_CHARS] for _, _, text in passages])
    with engine.begin() as conn:
        for (pid, doc_id, text), vec in zip(passages, vectors, strict=True):
            conn.exec_driver_sql(
                "INSERT INTO doc_fts(rowid, text, doc_id) VALUES (?, ?, ?)", (pid, text, doc_id)
            )
            conn.exec_driver_sql(
                f"INSERT INTO {table}(rowid, embedding, doc_id) VALUES (?, ?, ?)",
                (pid, sqlite_vec.serialize_float32(vec), doc_id),
            )


def delete_passages(passage_ids: list[int]) -> None:
    if not passage_ids:
        return
    engine = _engine()
    table = _ensure_doc_vec(engine, get_embedder().dim)
    with engine.begin() as conn:
        for pid in passage_ids:
            conn.exec_driver_sql("DELETE FROM doc_fts WHERE rowid = ?", (pid,))
            conn.exec_driver_sql(f"DELETE FROM {table} WHERE rowid = ?", (pid,))


def search_passages_bm25(doc_ids: list[str], query: str, *, k: int) -> list[int]:
    """Passage ids of the given documents matching `query` (BM25), best first."""
    q = fts_query(query)
    if not q or not doc_ids:
        return []
    marks = ",".join("?" for _ in doc_ids)
    with _engine().begin() as conn:
        rows = conn.exec_driver_sql(
            f"SELECT rowid FROM doc_fts WHERE doc_fts MATCH ? AND doc_id IN ({marks}) "
            "ORDER BY bm25(doc_fts) LIMIT ?",
            (q, *doc_ids, k),
        ).all()
    return [pid for (pid,) in rows]


def search_passages(doc_id: str, query: str, *, k: int) -> list[int]:
    """Passage ids of `doc_id` best matching `query` (keyword + vector, rank-fused)."""
    engine = _engine()
    pool = k * 4
    scores: dict[int, float] = {}

    def add(ids) -> None:
        for rank, pid in enumerate(ids):
            scores[pid] = scores.get(pid, 0.0) + 1.0 / (_RRF_K + rank + 1)

    with engine.begin() as conn:
        if q := fts_query(query):
            add(
                pid
                for (pid,) in conn.exec_driver_sql(
                    "SELECT rowid FROM doc_fts WHERE doc_fts MATCH ? AND doc_id = ? "
                    "ORDER BY bm25(doc_fts) LIMIT ?",
                    (q, doc_id, pool),
                ).all()
            )
        embedder = get_embedder()
        table = _ensure_doc_vec(engine, embedder.dim)
        [vec] = embedder.embed([query[:_EMBED_CHARS]])
        add(
            pid
            for (pid,) in conn.exec_driver_sql(
                f"SELECT rowid FROM {table} WHERE embedding MATCH ? AND k = ? AND doc_id = ? "
                "ORDER BY distance",
                (sqlite_vec.serialize_float32(vec), pool, doc_id),
            ).all()
        )
    return sorted(scores, key=lambda pid: -scores[pid])[:k]


# --- memory records (docs/MEMORY_CONTRACTS.md) ---------------------------------------------


def index_record(conn, record) -> None:
    """Index a memory record on `conn` — the caller's open transaction, so the record and its
    index row commit or roll back together."""
    conn.exec_driver_sql(
        "INSERT INTO record_fts(rowid, key, text, user_id, store, session_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            record.id,
            record.key.replace("_", " "),
            record.text,
            record.user_id,
            # enum value or the enum *name* SQLModel stores ("LONG_TERM") -> "long_term"
            str(getattr(record.store, "value", record.store)).lower(),
            record.session_id,
        ),
    )


def search_records(
    user_id: str, session_id: str, stores: set[str], query: str, *, k: int
) -> list[int]:
    """Record ids matching `query`, best first. Ownership filters run inside the query, before
    ranking: long_term = this user's; session = this user's records of this session."""
    q = fts_query(query)
    if not q or not stores:
        return []
    clauses, params = [], [q, user_id]
    if "long_term" in stores:
        clauses.append("store = 'long_term'")
    if "session" in stores:
        clauses.append("(store = 'session' AND session_id = ?)")
        params.append(session_id)
    sql = (
        "SELECT rowid FROM record_fts WHERE record_fts MATCH ? AND user_id = ? "
        f"AND ({' OR '.join(clauses)}) ORDER BY bm25(record_fts) LIMIT ?"
    )
    with _engine().begin() as conn:
        return [rid for (rid,) in conn.exec_driver_sql(sql, (*params, k)).all()]


def index_note(conn, note) -> None:
    """Index an episode note on the caller's open transaction (commits with the note)."""
    conn.exec_driver_sql(
        "INSERT INTO note_fts(rowid, text, user_id, session_id) VALUES (?, ?, ?, ?)",
        (note.id, note.text, note.user_id, note.session_id),
    )


def search_notes(user_id: str, session_id: str, query: str, *, k: int) -> list[int]:
    """Note ids of one session matching `query` (BM25), best first."""
    q = fts_query(query)
    if not q:
        return []
    with _engine().begin() as conn:
        rows = conn.exec_driver_sql(
            "SELECT rowid FROM note_fts WHERE note_fts MATCH ? AND user_id = ? "
            "AND session_id = ? ORDER BY bm25(note_fts) LIMIT ?",
            (q, user_id, session_id, k),
        ).all()
    return [nid for (nid,) in rows]


def vector_session_messages(
    session_id: str, query: str, *, k: int, max_distance: float
) -> list[int]:
    """Message ids of one session nearest to `query` by embedding, within `max_distance`."""
    engine = _engine()
    embedder = get_embedder()
    table = _ensure_vec(engine, embedder.dim)
    [vec] = embedder.embed([query[:_EMBED_CHARS]])
    with engine.begin() as conn:
        rows = conn.exec_driver_sql(
            f"SELECT rowid, distance FROM {table} WHERE embedding MATCH ? AND k = ? "
            "AND thread_id = ? ORDER BY distance",
            (sqlite_vec.serialize_float32(vec), k, session_id),
        ).all()
    return [mid for mid, dist in rows if dist <= max_distance]


def search_session_messages(session_id: str, query: str, *, k: int) -> list[int]:
    """Message ids of one session matching `query` (BM25), best first."""
    q = fts_query(query)
    if not q:
        return []
    with _engine().begin() as conn:
        rows = conn.exec_driver_sql(
            "SELECT rowid FROM message_fts WHERE message_fts MATCH ? AND thread_id = ? "
            "ORDER BY bm25(message_fts) LIMIT ?",
            (q, session_id, k),
        ).all()
    return [mid for (mid,) in rows]


def reindex_missing() -> dict[str, int]:
    """Durable repair: index messages and records that are missing from their indexes (e.g.
    after a crash between a message commit and its indexing). Safe to run any time."""
    engine = _engine()
    embedder = get_embedder()
    vec_table = _ensure_vec(engine, embedder.dim)
    with engine.begin() as conn:
        no_fts = conn.exec_driver_sql(
            "SELECT id, text, thread_id, turn FROM message "
            "WHERE id NOT IN (SELECT rowid FROM message_fts)"
        ).all()
        no_vec = conn.exec_driver_sql(
            f"SELECT id, text, thread_id, turn FROM message "
            f"WHERE id NOT IN (SELECT rowid FROM {vec_table})"
        ).all()
        no_note = conn.exec_driver_sql(
            "SELECT id, text, user_id, session_id FROM episodenote "
            "WHERE id NOT IN (SELECT rowid FROM note_fts)"
        ).all()
        for nid, text, user_id, session_id in no_note:
            index_note(
                conn, SimpleNamespace(id=nid, text=text, user_id=user_id, session_id=session_id)
            )
        no_rec = conn.exec_driver_sql(
            "SELECT id, key, text, user_id, store, session_id FROM memoryrecord "
            "WHERE id NOT IN (SELECT rowid FROM record_fts)"
        ).all()
        for mid, text, thread_id, turn in no_fts:
            conn.exec_driver_sql(
                "INSERT INTO message_fts(rowid, text, thread_id, turn) VALUES (?, ?, ?, ?)",
                (mid, text, thread_id, turn),
            )
        if no_vec:
            vectors = embedder.embed([text[:_EMBED_CHARS] for _, text, _, _ in no_vec])
            for (mid, _, thread_id, turn), vec in zip(no_vec, vectors, strict=True):
                conn.exec_driver_sql(
                    f"INSERT INTO {vec_table}(rowid, embedding, thread_id, turn) "
                    "VALUES (?, ?, ?, ?)",
                    (mid, sqlite_vec.serialize_float32(vec), thread_id, turn),
                )
        for rid, key, text, user_id, store, session_id in no_rec:
            row = SimpleNamespace(
                id=rid, key=key, text=text, user_id=user_id, store=store, session_id=session_id
            )
            index_record(conn, row)
    return {
        "message_fts": len(no_fts),
        "message_vec": len(no_vec),
        "record_fts": len(no_rec),
        "note_fts": len(no_note),
    }


@cache
def repair_once(engine: Engine) -> dict[str, int]:
    """Run `reindex_missing` once per database per process (startup self-healing)."""
    return reindex_missing()
