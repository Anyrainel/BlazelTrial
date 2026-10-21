"""Deterministic retrieval tests for the source + note indexes (PLAN sequence step 2)."""

import pytest

from chatbot import core
from chatbot.memory import messages as store
from chatbot.memory import records, refs, search
from chatbot.memory.db import get_engine, session
from chatbot.memory.models import EpisodeNote, NoteSource
from chatbot.memory.records import MAX_EXCERPT, MAX_RESULT_CHARS, MemoryAccessError, MemoryCaller

A = MemoryCaller("me", "A")
B = MemoryCaller("me", "B")

FILLER = [
    "Standup ran long again today.",
    "Had ramen for lunch.",
    "Reviewing the billing revamp spec this afternoon.",
    "Rainy again, third day in a row.",
]


def chat(caller: MemoryCaller, turns: list[tuple[str, str]]) -> list[int]:
    """Ingest (user, assistant) turns; return the new message ids in order."""
    before = {m.id for m in store.thread_messages(caller.session_id)}
    for user, assistant in turns:
        core.ingest(caller.user_id, caller.session_id, user, reply=assistant)
    return [m.id for m in store.thread_messages(caller.session_id) if m.id not in before]


def msg_with(caller: MemoryCaller, needle: str) -> int:
    return next(m.id for m in store.thread_messages(caller.session_id) if needle in m.text)


def filler(n: int) -> list[tuple[str, str]]:
    return [(FILLER[i % len(FILLER)], "Noted.") for i in range(n)]


# --- the four retrieval properties ---------------------------------------------------------------


def test_raw_only_details_are_found_even_when_the_note_omits_them():
    ids = chat(
        A,
        [*filler(3), ("Tasting is at 4:30pm on June 18 at Saffron Table.", "Got it."), *filler(3)],
    )
    records.add_note("me", "A", ids, "The user planned catering for Dana's farewell.")

    hits = records.search_memory(A, "what time is the tasting")
    assert hits[0].type == "message" and "4:30pm" in hits[0].excerpt
    assert "bm25:messages" in hits[0].found_by


def test_notes_bridge_paraphrases_and_lead_back_to_the_exact_source():
    ids = chat(
        A,
        [
            *filler(2),
            ("Saffron Table will handle the food for Dana's send-off.", "Great."),
            *filler(2),
        ],
    )
    note = records.add_note(
        "me", "A", ids, "The user booked Saffron Table as caterer for Dana's farewell party."
    )
    query = "Which caterer did I book for the farewell party?"

    raw_only = records.search_memory(A, query, sources=("messages",))
    assert not any("Saffron" in h.excerpt for h in raw_only)  # lexical raw search misses it

    hits = records.search_memory(A, query)
    assert hits[0].ref == note and hits[0].type == "note"
    covered = records.read(A, note).covers
    source = refs.msg(msg_with(A, "send-off"))
    assert source in covered  # the note leads to the exact evidence...
    assert "Saffron Table will handle the food" in records.read(A, source).text  # ...verbatim


def test_adjacent_context_resolves_what_a_terse_hit_refers_to():
    chat(
        A,
        [
            *filler(2),
            (
                "Should the tasting be on the 18th or the 25th?",
                "The 18th works better for Saffron.",
            ),
            ("OK, lock it in.", "Done."),
            *filler(2),
        ],
    )
    [hit] = [h for h in records.search_memory(A, "lock it in") if h.type == "message"][:1]
    around = records.expand(A, hit.ref, before=2, after=0)
    text = " ".join(v.text for v in around)
    assert "18th or the 25th" in text and "18th works better" in text


def test_misleading_notes_never_hide_the_raw_evidence():
    ids = chat(
        A,
        [
            *filler(2),
            (
                "Someone floated October 6, but the launch stays September 29, confirmed by Priya.",
                "Noted.",
            ),
            *filler(2),
        ],
    )
    note = records.add_note("me", "A", ids, "Launch date moved to October 6.")

    hits = records.search_memory(A, "launch date")
    by_type = {h.type: h for h in hits}
    assert {"note", "message"} <= set(by_type)  # both representations returned
    assert "September 29" in by_type["message"].excerpt
    assert (
        by_type["note"].covers and refs.msg(msg_with(A, "floated")) in records.read(A, note).covers
    )


# --- ownership, validation, bounds ---------------------------------------------------------------


def test_notes_are_session_private():
    ids = chat(B, [("Session B secret: the vault code is 4412.", "ok")])
    note = records.add_note("me", "B", ids, "Vault code discussed in B.")
    assert records.search_memory(A, "vault code") == []
    with pytest.raises(MemoryAccessError):
        records.read(A, note)
    assert records.search_memory(B, "vault code")


@pytest.mark.parametrize("case", ["empty", "gap", "overlap", "other_session", "too_long"])
def test_note_writer_validates_segments(case):
    ids = chat(A, filler(4))
    other = chat(B, filler(1))
    records.add_note("me", "A", ids[:2], "first segment")
    bad = {
        "empty": ([], "x"),
        "gap": ([ids[2], ids[4]], "x"),  # skips ids[3]
        "overlap": (ids[1:3], "x"),
        "other_session": (other, "x"),
        "too_long": (ids[2:], "x" * 1501),
    }[case]
    with pytest.raises(MemoryAccessError):
        records.add_note("me", "A", *bad)


def test_one_hit_per_turn_and_bounded_output():
    chat(
        A,
        [
            (
                f"Saffron Table detail number {i}. " + "padding " * 80,
                "Saffron Table noted. " + "z " * 80,
            )
            for i in range(12)
        ],
    )
    hits = records.search_memory(A, "Saffron Table", k=50)
    assert len(hits) <= records.MAX_K
    turns = [h.turn for h in hits if h.type == "message"]
    assert len(turns) == len(set(turns))  # user + assistant of a turn collapse into one hit
    assert all(len(h.excerpt) <= MAX_EXCERPT + len("assistant: ") for h in hits)
    assert sum(len(h.excerpt) for h in hits) <= MAX_RESULT_CHARS


def test_optional_vector_candidates_are_tagged_and_filtered_by_session():
    chat(A, [("Saffron Table caters the farewell.", "ok")])
    chat(B, [("Saffron Table also in B.", "ok")])
    hits = records.search_memory(A, "Saffron Table farewell", sources=("messages", "vectors"))
    assert hits and all("in B" not in h.excerpt for h in hits)
    assert any("vectors:messages" in h.found_by for h in hits)
    with pytest.raises(MemoryAccessError, match="unknown sources"):
        records.search_memory(A, "x", sources=("everything",))


def test_note_and_index_row_commit_together(monkeypatch):
    ids = chat(A, filler(2))

    def boom(conn, note):
        raise RuntimeError("index down")

    monkeypatch.setattr(search, "index_note", boom)
    with pytest.raises(RuntimeError):
        records.add_note("me", "A", ids, "never stored")
    with session() as s:
        assert s.exec(EpisodeNote.__table__.select()).all() == []
        assert s.exec(NoteSource.__table__.select()).all() == []


def test_reindex_repairs_missing_note_rows():
    ids = chat(A, filler(2))
    note = records.add_note("me", "A", ids, "Weather and lunch chatter.")
    with get_engine().begin() as conn:
        conn.exec_driver_sql("DELETE FROM note_fts")
    assert not any(h.ref == note for h in records.search_memory(A, "weather chatter"))
    assert search.reindex_missing()["note_fts"] == 1
    assert any(h.ref == note for h in records.search_memory(A, "weather chatter"))
