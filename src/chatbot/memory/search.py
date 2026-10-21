"""Hybrid search over past messages: FTS5 keyword ranking + sqlite-vec similarity, fused by
reciprocal rank. The only module with raw SQL (virtual tables SQLModel can't express)."""

import re
from dataclasses import dataclass
from functools import cache

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
