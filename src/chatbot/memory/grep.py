"""Literal substring scan over stored text: finds exact strings that keyword (BM25) search
ranks badly (numbers, codes, partial names) and shows what a forget would delete.

Matching is the same as forget's (case- and whitespace-insensitive), so "what grep finds" and
"what erase removes" agree. Scoped scans see what the caller may see; the user-wide scan (used
only while forgetting, which is user-wide by contract) covers every conversation.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime

from sqlmodel import col, select

from chatbot.memory import refs
from chatbot.memory.db import session
from chatbot.memory.forget import matcher
from chatbot.memory.models import (
    Checkpoint,
    EpisodeNote,
    GeneratedPost,
    MemoryRecord,
    Message,
    OutboundMessage,
    Role,
    Store,
    ToolOutput,
)

EXCERPT_CHARS = 160
MAX_TOTAL_CHARS = 2400


@dataclass(frozen=True)
class Mention:
    ref: str
    kind: str  # message | record | note | saved | outbound
    where: str
    when: datetime
    excerpt: str


@dataclass
class Found:
    counts: dict[str, int] = field(default_factory=dict)
    mentions: list[Mention] = field(default_factory=list)

    def total(self) -> int:
        return sum(self.counts.values())

    def render(self) -> str:
        if not self.counts:
            return "No matches."
        head = "Matches: " + ", ".join(f"{n} {kind}" for kind, n in self.counts.items())
        lines = [f"{m.ref} ({m.where}, {m.when:%Y-%m-%d}): {m.excerpt}" for m in self.mentions]
        return "\n".join([head, *lines])


def _excerpt(text: str, pattern: re.Pattern) -> str:
    hit = pattern.search(text)
    start = max(0, (hit.start() if hit else 0) - EXCERPT_CHARS // 2)
    snippet = " ".join(text[start : start + EXCERPT_CHARS].split())
    return ("…" if start else "") + snippet + ("…" if start + EXCERPT_CHARS < len(text) else "")


def find(
    user_id: str,
    text: str,
    *,
    session_id: str | None = None,
    stores: frozenset[str] = frozenset({"session", "long_term"}),
    k: int = 8,
) -> Found:
    """Where `text` occurs. With `session_id`: only what that session may see (its messages and
    notes, its session records, the user's long-term records). Without: everything the user has."""
    found = Found()
    value = " ".join(text.split())
    if not value:
        return found
    pattern = matcher([value])
    user_wide = session_id is None
    candidates: list[Mention] = []

    def add(kind: str, items: list[Mention]) -> None:
        if items:
            found.counts[kind] = found.counts.get(kind, 0) + len(items)
            candidates.extend(items)

    with session() as s:
        if "session" in stores:
            query = select(Message).where(Message.user_id == user_id, Message.forgotten == False)  # noqa: E712
            if not user_wide:
                query = query.where(Message.thread_id == session_id)
            add(
                "message",
                [
                    Mention(
                        refs.msg(m.id),
                        "message",
                        f"{'user' if m.role == Role.USER else 'assistant'}, {m.thread_id}",
                        m.created_at,
                        _excerpt(m.text, pattern),
                    )
                    for m in s.exec(query.order_by(col(Message.id).desc())).all()
                    if pattern.search(m.text)
                ],
            )
            notes = select(EpisodeNote).where(EpisodeNote.user_id == user_id)
            if not user_wide:
                notes = notes.where(EpisodeNote.session_id == session_id)
            add(
                "note",
                [
                    Mention(
                        refs.note(n.id),
                        "note",
                        n.session_id,
                        n.created_at,
                        _excerpt(n.text, pattern),
                    )
                    for n in s.exec(notes.order_by(col(EpisodeNote.id).desc())).all()
                    if pattern.search(n.text)
                ],
            )
        wanted = {Store(x) for x in stores}
        recs = select(MemoryRecord).where(MemoryRecord.user_id == user_id)
        add(
            "record",
            [
                Mention(
                    refs.rec(r.id),
                    "record",
                    f"{r.store.value} {r.kind.value} '{r.key}', "
                    f"{'current' if r.superseded_at is None else 'superseded'}",
                    r.created_at,
                    _excerpt(r.text, pattern),
                )
                for r in s.exec(recs.order_by(col(MemoryRecord.id).desc())).all()
                if r.store in wanted
                and (user_wide or r.store == Store.LONG_TERM or r.session_id == session_id)
                and pattern.search(r.text)
            ],
        )
        if user_wide:  # stored by-products of runs: only the forget sweep needs to see these
            for model, label, attr in (
                (ToolOutput, "saved output", "content"),
                (Checkpoint, "checkpoint", "text"),
            ):
                rows = s.exec(select(model).where(model.user_id == user_id)).all()
                add(
                    "saved",
                    [
                        Mention(
                            f"{'out' if model is ToolOutput else 'ckpt'}:{r.id}",
                            "saved",
                            label,
                            r.created_at,
                            _excerpt(getattr(r, attr), pattern),
                        )
                        for r in rows
                        if pattern.search(getattr(r, attr))
                    ],
                )
            sent = s.exec(select(OutboundMessage)).all()
            add(
                "outbound",
                [
                    Mention(
                        f"sent:{o.id}",
                        "outbound",
                        f"to {o.recipient}",
                        o.created_at,
                        _excerpt(o.text, pattern),
                    )
                    for o in sent
                    if pattern.search(o.text) or pattern.search(o.recipient)
                ],
            )
            add(
                "outbound",
                [
                    Mention(
                        f"post:{p.id}",
                        "outbound",
                        "LinkedIn post",
                        p.created_at,
                        _excerpt(p.text, pattern),
                    )
                    for p in s.exec(select(GeneratedPost)).all()
                    if pattern.search(p.text)
                ],
            )

    used = 0
    for mention in sorted(candidates, key=lambda m: m.when, reverse=True):
        if len(found.mentions) >= k or used + len(mention.excerpt) > MAX_TOTAL_CHARS:
            break
        used += len(mention.excerpt)
        found.mentions.append(mention)
    return found


def message_ids(user_id: str, text: str) -> list[int]:
    """Ids of every message of the user containing `text` (what erasing it deletes)."""
    value = " ".join(text.split())
    if not value:
        return []
    pattern = matcher([value])
    with session() as s:
        rows = s.exec(
            select(Message).where(Message.user_id == user_id, Message.forgotten == False)  # noqa: E712
        ).all()
    return [m.id for m in rows if pattern.search(m.text)]
