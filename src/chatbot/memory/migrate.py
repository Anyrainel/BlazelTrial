"""Explicit migration of legacy `Fact` rows into `MemoryRecord` (docs/MEMORY_CONTRACTS.md §8).

Incremental and idempotent: a migrated record keeps `legacy_fact_id` (unique), so a rerun
migrates only facts created since, and links version chains that span both runs. Facts are
copied, never deleted; the old table stays until the injection path is removed.
"""

from sqlmodel import col, select

from chatbot.memory import refs, search
from chatbot.memory.db import session
from chatbot.memory.models import (
    Fact,
    FactKind,
    MemoryRecord,
    Message,
    RecordKind,
    RecordSource,
    Role,
    Scope,
    Store,
)
from chatbot.memory.records import MAX_EXCERPT, _clip, _write_lock


def migrate_facts() -> dict[str, int]:
    """Copy not-yet-migrated facts; returns counts (migrated, without_source, chains_linked)."""
    counts = {"migrated": 0, "without_source": 0, "chains_linked": 0}
    with _write_lock, session() as s:
        done = set(
            s.exec(
                select(MemoryRecord.legacy_fact_id).where(
                    col(MemoryRecord.legacy_fact_id).is_not(None)
                )
            )
        )
        facts = s.exec(select(Fact).order_by(col(Fact.id))).all()
        for fact in facts:
            if fact.id in done:
                continue
            sources = s.exec(
                select(Message).where(
                    Message.thread_id == fact.source_thread_id,
                    Message.turn == fact.source_turn,
                    Message.role == Role.USER,
                )
            ).all()
            record = MemoryRecord(
                user_id=fact.user_id,
                store=Store.LONG_TERM if fact.scope == Scope.USER else Store.SESSION,
                session_id=fact.thread_id if fact.scope == Scope.THREAD else None,
                origin_session_id=fact.source_thread_id,
                kind=RecordKind.PREFERENCE if fact.kind == FactKind.PREFERENCE else RecordKind.FACT,
                key=fact.key,
                text=fact.text,
                created_at=fact.created_at,
                superseded_at=fact.superseded_at,
                legacy_fact_id=fact.id,
            )
            s.add(record)
            s.flush()
            for m in sources:
                s.add(
                    RecordSource(
                        record_id=record.id, ref=refs.msg(m.id), excerpt=_clip(m.text, MAX_EXCERPT)
                    )
                )
            search.index_record(s.connection(), record)
            counts["migrated"] += 1
            counts["without_source"] += not sources

        # Version links between migrated records, including chains spanning reruns.
        by_fact = {
            r.legacy_fact_id: r
            for r in s.exec(
                select(MemoryRecord).where(col(MemoryRecord.legacy_fact_id).is_not(None))
            )
        }
        for fact in facts:
            if fact.superseded_by is None:
                continue
            old, new = by_fact.get(fact.id), by_fact.get(fact.superseded_by)
            if old is None or new is None or old.superseded_by == new.id:
                continue
            old.superseded_by, old.superseded_at = new.id, fact.superseded_at
            new.supersedes_id = old.id
            s.add(old)
            s.add(new)
            counts["chains_linked"] += 1
        s.commit()
    return counts
