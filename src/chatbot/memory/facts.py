"""Fact store: scoped, versioned facts and the profile block injected into chat prompts."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlmodel import col, or_, select

from chatbot.memory.db import session
from chatbot.memory.models import Fact, Scope, utcnow


@dataclass(frozen=True)
class NewFact:
    scope: Scope
    key: str
    text: str


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


def render_profile(user_id: str, thread_id: str) -> str:
    """Prompt block of what we know. Current facts first; earlier versions listed under each."""
    active = active_facts(user_id, thread_id)
    if not active:
        return ""
    history: dict[tuple[Scope, str], list[Fact]] = {}
    for old in superseded_facts(user_id, thread_id, {f.key for f in active}):
        history.setdefault((old.scope, old.key), []).append(old)

    def lines(scope: Scope) -> list[str]:
        out = []
        for f in (f for f in active if f.scope == scope):
            out.append(f"- [{f.key}] {f.text} (since {_when(f.created_at)})")
            for old in history.get((f.scope, f.key), []):
                until = _when(old.superseded_at) if old.superseded_at else "?"
                out.append(f"  - earlier: {old.text} ({_when(old.created_at)} until {until})")
        return out

    blocks = []
    if user_lines := lines(Scope.USER):
        blocks.append(
            "## What you know about the user (all conversations)\n" + "\n".join(user_lines)
        )
    if thread_lines := lines(Scope.THREAD):
        blocks.append("## Notes for this conversation only\n" + "\n".join(thread_lines))
    return "\n\n".join(blocks)
