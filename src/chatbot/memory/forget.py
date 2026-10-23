"""Forget: take given values out of memory everywhere for a user.

The chat history is not memory and is kept. In soft mode (default) a message that contains a
value is only flagged `forgotten`: it stays in the conversation as written, but no memory path
may use it (not indexed, searched, read, replayed or summarized). Everything *derived* from it is
truly deleted. Hard mode (opt-in) also deletes the messages and purges the bytes.

Deterministic and user-wide (every session), serialized with all other memory writes:
- messages containing a value are flagged (soft) or deleted (hard), with their index rows and
  stored runs; stored runs of the remaining messages have the value redacted (tool args, tool
  results, replies);
- memory records containing a value, or whose original evidence was all forgotten, are deleted;
  other cited excerpts containing it are dropped; version links skip deleted versions;
- episode notes containing a value or covering a forgotten message are deleted;
- saved tool outputs, checkpoints, outbound messages and legacy fact/summary rows containing a
  value are deleted;
- `forget_session` applies this to one whole session: all its messages (but the current request),
  its session-store records, saved outputs, checkpoints, sent messages and summaries;
- FTS indexes are optimized, the WAL is truncated and the database is VACUUMed, so the deleted
  bytes are gone from disk too.
No separate list of forgotten values is persisted.
"""

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlmodel import col, select

from chatbot.config import get_settings
from chatbot.memory import refs, search
from chatbot.memory.db import get_engine, session
from chatbot.memory.models import (
    Checkpoint,
    EpisodeNote,
    Fact,
    MemoryRecord,
    Message,
    NoteSource,
    OutboundMessage,
    RecordSource,
    Role,
    SlackDelivery,
    Store,
    Summary,
    ToolOutput,
)
from chatbot.memory.records import _write_lock

REDACTED = "[forgotten]"


@dataclass
class ForgetReport:
    messages: int = 0
    runs_redacted: int = 0
    records: int = 0
    sources: int = 0
    notes: int = 0
    tool_outputs: int = 0
    checkpoints: int = 0
    outbound: int = 0
    legacy: int = 0
    rejected: list[str] = field(default_factory=list)

    def total(self) -> int:
        return sum(v for v in vars(self).values() if isinstance(v, int))


def clean_values(values: list[str]) -> tuple[list[str], list[str]]:
    """Normalize literal values; only empty values are invalid, regardless of length."""
    keep, rejected = [], []
    for v in values:
        v = " ".join(v.split())
        (keep if v else rejected).append(v)
    return list(dict.fromkeys(keep)), rejected


def matcher(values: list[str]) -> re.Pattern:
    """Whitespace- and case-insensitive literal match of any value (no values: matches nothing)."""
    parts = [r"\s+".join(map(re.escape, v.split())) for v in values if v.split()]
    return re.compile("|".join(parts), re.IGNORECASE) if parts else re.compile(r"(?!)")


def redact(text: str, values: list[str]) -> str:
    return matcher(values).sub(REDACTED, text) if values and text else text


def _redact_json(raw: str, pattern: re.Pattern) -> str:
    """Redact inside every string of a JSON document (keeps it valid JSON)."""

    def walk(node):
        if isinstance(node, str):
            return pattern.sub(REDACTED, node)
        if isinstance(node, list):
            return [walk(x) for x in node]
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        return node

    return json.dumps(walk(json.loads(raw)))


def forget_values(
    user_id: str,
    values: list[str],
    *,
    message_ids: Iterable[int] = (),
    compact: bool = True,
    mode: str | None = None,
    session_id: str | None = None,
) -> ForgetReport:
    """Forget `values` and whole turns of `message_ids` using the configured mode. With
    `session_id`, also everything stored for that session (see `forget_session`)."""
    mode = mode or get_settings().memory_forget_mode
    if mode not in {"soft", "hard"}:
        raise ValueError("forget mode must be soft or hard")
    values, rejected = clean_values(values)
    explicit = set(message_ids)
    report = ForgetReport(rejected=rejected)
    if not values and not explicit and not session_id:
        return report
    hit = matcher(values)
    matches = lambda text: bool(text) and bool(hit.search(text))  # noqa: E731
    in_session = lambda row_session: bool(session_id) and row_session == session_id  # noqa: E731

    with _write_lock, session() as s:
        # 1. messages: flagged in soft mode (the chat keeps them), deleted in hard mode
        msgs = s.exec(select(Message).where(Message.user_id == user_id)).all()
        turns = {(m.thread_id, m.turn) for m in msgs if m.id in explicit}
        gone_msgs = [
            m
            for m in msgs
            if (matches(m.text) or (m.thread_id, m.turn) in turns)
            and (mode == "hard" or not m.forgotten)
        ]
        gone_ids = {m.id for m in gone_msgs}
        for delivery in s.exec(
            select(SlackDelivery).where(col(SlackDelivery.reply_message_id).in_(gone_ids))
        ).all():
            delivery.reply_message_id = None
            delivery.delivered = True
            s.add(delivery)
        # Keep a safe completion boundary when deleting a fulfilled reply. Otherwise
        # the surviving request replays as unfinished work (including draft-only rules).
        surviving_users = {
            (m.thread_id, m.turn) for m in msgs if m.role == Role.USER and m.id not in gone_ids
        }
        for m in gone_msgs:
            if (
                mode == "hard"
                and m.role == Role.ASSISTANT
                and (m.thread_id, m.turn) in surviving_users
            ):
                s.add(
                    Message(
                        thread_id=m.thread_id,
                        user_id=m.user_id,
                        turn=m.turn,
                        role=Role.ASSISTANT,
                        source=m.source,
                        text="[This request was completed. Its reply was removed by forgetting.]",
                        created_at=m.created_at,
                    )
                )
        for m in msgs:
            if m.id in gone_ids:
                if mode == "soft":
                    m.forgotten = True
                    m.run_messages_json = None  # the run echoes tool results and memory answers
                    s.add(m)
                else:
                    s.delete(m)
            elif m.run_messages_json and matches(m.run_messages_json):
                m.run_messages_json = _redact_json(m.run_messages_json, hit)
                s.add(m)
                report.runs_redacted += 1
        report.messages = len(gone_ids)
        gone_refs = {refs.msg(i) for i in gone_ids | {m.id for m in msgs if m.forgotten}}

        # 2. memory records and their sources
        recs = s.exec(select(MemoryRecord).where(MemoryRecord.user_id == user_id)).all()
        sources = {
            r.id: s.exec(select(RecordSource).where(RecordSource.record_id == r.id)).all()
            for r in recs
        }
        doomed: set[int] = set()
        for r in recs:
            evidence = [
                x
                for x in sources[r.id]
                if refs.parse(x.ref) and refs.parse(x.ref).type in refs.EVIDENCE
            ]
            orphaned = evidence and all(x.ref in gone_refs for x in evidence)
            own = r.store == Store.SESSION and in_session(r.session_id)
            if matches(r.text) or matches(r.key) or orphaned or own:
                doomed.add(r.id)
        for r in recs:
            for x in sources[r.id]:
                if (
                    r.id in doomed
                    or x.ref in gone_refs
                    or matches(x.excerpt)
                    or (x.ref.startswith("rec:") and refs.parse(x.ref).id in doomed)
                ):
                    s.delete(x)
                    report.sources += 1
        s.flush()  # dependent rows first: RecordSource has a foreign key to its record
        for r in recs:
            if r.id in doomed:
                continue
            if r.supersedes_id in doomed:  # skip deleted versions in the chain
                r.supersedes_id = _first_kept(recs, r.supersedes_id, doomed, back=True)
            if r.superseded_by in doomed:
                r.superseded_by = _first_kept(recs, r.superseded_by, doomed, back=False)
            s.add(r)
        for r in recs:
            if r.id in doomed:
                s.delete(r)
        report.records = len(doomed)

        # 3. notes: containing a value, or derived from a deleted message
        notes = s.exec(select(EpisodeNote).where(EpisodeNote.user_id == user_id)).all()
        gone_notes = []
        for n in notes:
            covered = set(
                s.exec(select(NoteSource.message_id).where(NoteSource.note_id == n.id)).all()
            )
            if matches(n.text) or covered & gone_ids or in_session(n.session_id):
                for ns in s.exec(select(NoteSource).where(NoteSource.note_id == n.id)).all():
                    s.delete(ns)
                s.flush()
                s.delete(n)
                gone_notes.append(n.id)
        report.notes = len(gone_notes)

        # 4. other stored state
        for model, attr, field_name in (
            (ToolOutput, "tool_outputs", "content"),
            (Checkpoint, "checkpoints", "text"),
        ):
            for row in s.exec(select(model).where(model.user_id == user_id)).all():
                if matches(getattr(row, field_name)) or in_session(row.session_id):
                    s.delete(row)
                    setattr(report, attr, getattr(report, attr) + 1)
        threads = {m.thread_id for m in msgs}
        for row in s.exec(
            select(OutboundMessage).where(col(OutboundMessage.thread_id).in_(threads))
        ).all():
            if matches(row.text) or matches(row.recipient) or in_session(row.thread_id):
                s.delete(row)
                report.outbound += 1
        for row in s.exec(select(Fact).where(Fact.user_id == user_id)).all():
            if matches(row.text):
                s.delete(row)
                report.legacy += 1
        for row in s.exec(select(Summary).where(col(Summary.thread_id).in_(threads))).all():
            if matches(row.text) or in_session(row.thread_id):
                s.delete(row)
                report.legacy += 1

        # 5. index rows, in the same transaction
        conn = s.connection()
        search.delete_index_rows(conn, messages=gone_ids, records=doomed, notes=set(gone_notes))
        s.commit()

    if compact and report.total():
        compact_storage()
    return report


def forget_session(
    user_id: str,
    session_id: str,
    *,
    keep_message_id: int | None = None,
    compact: bool = True,
    mode: str | None = None,
) -> ForgetReport:
    """Forget everything said in one session, with no value to search for. The turn of
    `keep_message_id` (the request to forget, still being answered) is left alone."""
    with session() as s:
        msgs = s.exec(
            select(Message).where(Message.user_id == user_id, Message.thread_id == session_id)
        ).all()
    keep_turns = {m.turn for m in msgs if m.id == keep_message_id}
    ids = [m.id for m in msgs if m.turn not in keep_turns]
    return forget_values(
        user_id, [], message_ids=ids, compact=compact, mode=mode, session_id=session_id
    )


def _first_kept(recs, rid, doomed, *, back: bool):
    by_id = {r.id: r for r in recs}
    while rid in doomed:
        rid = by_id[rid].supersedes_id if back else by_id[rid].superseded_by
    return rid


def compact_storage() -> None:
    """Purge deleted bytes: merge FTS segments, checkpoint the WAL, VACUUM."""
    engine = get_engine()
    with engine.begin() as conn:
        for table in ("message_fts", "record_fts", "note_fts", "doc_fts"):
            conn.exec_driver_sql(f"INSERT INTO {table}({table}) VALUES ('optimize')")
    with engine.connect() as conn:
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")
        conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.exec_driver_sql("VACUUM")
        conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
