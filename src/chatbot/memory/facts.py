"""Fact store: scoped, versioned facts and the memory blocks injected into chat prompts.

Preferences (few, must apply to everything) form a stable block in the instructions.
Facts can number in the hundreds, so only those most relevant to the current message are
shown, within a size budget, in the volatile part of the prompt."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlmodel import col, or_, select

from chatbot.memory.db import session
from chatbot.memory.embeddings import get_embedder
from chatbot.memory.models import Fact, FactKind, Scope, utcnow
from chatbot.memory.search import fts_query


@dataclass(frozen=True)
class NewFact:
    scope: Scope
    key: str
    text: str
    kind: FactKind = FactKind.FACT


def _visible(user_id: str, thread_id: str):
    """User-scope facts plus this thread's thread-scope facts."""
    return (
        Fact.user_id == user_id,
        or_(
            Fact.scope == Scope.USER,
            (Fact.scope == Scope.THREAD) & (Fact.thread_id == thread_id),
        ),
    )


def active_facts(user_id: str, thread_id: str) -> list[Fact]:
    with session() as s:
        return list(
            s.exec(
                select(Fact)
                .where(*_visible(user_id, thread_id), col(Fact.superseded_at).is_(None))
                .order_by(col(Fact.id))
            ).all()
        )


def superseded_facts(user_id: str, thread_id: str, keys: Iterable[str]) -> list[Fact]:
    keys = list(keys)
    if not keys:
        return []
    with session() as s:
        return list(
            s.exec(
                select(Fact)
                .where(
                    *_visible(user_id, thread_id),
                    col(Fact.superseded_at).is_not(None),
                    col(Fact.key).in_(keys),
                )
                .order_by(col(Fact.id))
            ).all()
        )


def normalize_key(key: str) -> str:
    return "_".join(key.strip().lower().replace("-", " ").split())


def apply(user_id: str, thread_id: str, turn: int, facts: Iterable[NewFact]) -> list[Fact]:
    """Store new facts. A fact whose key matches an active fact in the same scope supersedes it;
    an identical restatement is a no-op. Returns the facts actually added."""
    added: list[Fact] = []
    with session() as s:
        for new in facts:
            key = normalize_key(new.key)
            fact_thread = thread_id if new.scope == Scope.THREAD else None
            current = s.exec(
                select(Fact).where(
                    Fact.user_id == user_id,
                    Fact.scope == new.scope,
                    Fact.thread_id == fact_thread,
                    Fact.key == key,
                    col(Fact.superseded_at).is_(None),
                )
            ).first()
            if current is not None and current.text.strip() == new.text.strip():
                continue
            fact = Fact(
                user_id=user_id,
                scope=new.scope,
                thread_id=fact_thread,
                key=key,
                kind=new.kind,
                text=new.text.strip(),
                source_thread_id=thread_id,
                source_turn=turn,
            )
            s.add(fact)
            s.flush()
            if current is not None:
                current.superseded_at = utcnow()
                current.superseded_by = fact.id
                s.add(current)
            added.append(fact)
        s.commit()
    return added


def _when(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d %H:%M")


def _history(user_id: str, thread_id: str, facts: list[Fact]) -> dict[tuple, list[Fact]]:
    history: dict[tuple, list[Fact]] = {}
    for old in superseded_facts(user_id, thread_id, {f.key for f in facts}):
        history.setdefault((old.scope, old.key), []).append(old)
    return history


def _lines(fact: Fact, history: dict[tuple, list[Fact]], *, local_tag: bool = False) -> str:
    tag = " (this conversation only)" if local_tag and fact.scope == Scope.THREAD else ""
    out = [f"- [{fact.key}] {fact.text}{tag} (since {_when(fact.created_at)})"]
    for old in history.get((fact.scope, fact.key), []):
        until = _when(old.superseded_at) if old.superseded_at else "?"
        out.append(f"  - earlier: {old.text} ({_when(old.created_at)} until {until})")
    return "\n".join(out)


def render_preferences(user_id: str, thread_id: str, max_chars: int = 4000) -> str:
    """Standing instructions with their earlier versions. Stable between changes (cacheable)."""
    prefs = [f for f in active_facts(user_id, thread_id) if f.kind == FactKind.PREFERENCE]
    if not prefs:
        return ""
    history = _history(user_id, thread_id, prefs)
    blocks = []
    for scope, title in (
        (Scope.USER, "## Standing instructions from the user (all conversations)"),
        (Scope.THREAD, "## Instructions for this conversation only"),
    ):
        lines = [_lines(f, history) for f in prefs if f.scope == scope]
        if lines:
            blocks.append(title + "\n" + "\n".join(lines))
    return "\n\n".join(blocks)[:max_chars]


# Fact text never changes (updates create new facts), so text -> vector is a safe cache key.
_fact_vectors: dict[tuple[int, str], list[float]] = {}


def _vectors(facts: list[Fact]) -> list[list[float]]:
    dim = get_embedder().dim
    missing = [f for f in facts if (dim, f.text) not in _fact_vectors]
    if missing:
        for f, vec in zip(missing, get_embedder().embed([f.text for f in missing]), strict=True):
            _fact_vectors[(dim, f.text)] = vec
    return [_fact_vectors[(dim, f.text)] for f in facts]


def _relevance(query: str, facts: list[Fact]) -> list[float]:
    """Cosine similarity of fact text to the query, plus a bonus per shared distinctive word."""
    [qvec] = get_embedder().embed([query])
    words = set((fts_query(query) or "").replace('"', "").split(" OR ")) - {""}
    scores = []
    for f, vec in zip(facts, _vectors(facts), strict=True):
        cos = sum(a * b for a, b in zip(qvec, vec, strict=True))
        text = f"{f.key.replace('_', ' ')} {f.text}".lower()
        scores.append(cos + 0.2 * sum(1 for w in words if w in text))
    return scores


def render_relevant_facts(user_id: str, thread_id: str, query: str, max_chars: int) -> str:
    """Facts most relevant to `query` (with earlier versions), best first, within `max_chars`."""
    facts = [f for f in active_facts(user_id, thread_id) if f.kind == FactKind.FACT]
    if not facts:
        return ""
    ranked = sorted(zip(_relevance(query, facts), facts, strict=True), key=lambda x: -x[0])
    history = _history(user_id, thread_id, facts)
    chosen, used = [], 0
    for _, fact in ranked:
        line = _lines(fact, history, local_tag=True)
        if used + len(line) > max_chars:
            break
        chosen.append(line)
        used += len(line)
    return "\n".join(chosen)
