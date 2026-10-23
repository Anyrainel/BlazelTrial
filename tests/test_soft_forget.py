"""Soft forget: the chat history stays as written, memory never sees it again."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from chatbot import core
from chatbot.agent.agent import get_agent
from chatbot.agent.models import build_model
from chatbot.capabilities.memory.subagent import Mode
from chatbot.config import get_settings
from chatbot.memory import forget, notes, records, refs, search
from chatbot.memory import messages as store
from chatbot.memory.db import _add_missing_columns, session
from chatbot.memory.grep import find
from chatbot.memory.models import EpisodeNote, Message
from chatbot.memory.records import MemoryAccessError, MemoryCaller
from chatbot.surfaces.http import app
from tests.fakes import scripted, worker_answering

SECRET = "Velvet Otter access code 873129"
CALLER = MemoryCaller("me", "A")


def seed():
    core.ingest("me", "A", SECRET, reply=f"Noted: {SECRET}")
    core.ingest("me", "A", "Unrelated: I like tea.", reply="Good choice.")
    original = store.thread_messages("A")[0]
    record = records.write(
        CALLER,
        store="long_term",
        kind="fact",
        key="code",
        text=SECRET,
        sources=[refs.msg(original.id)],
    )
    records.save_tool_output(CALLER, "call-1", "ask_memory", f"answer: {SECRET}")
    return original, record


def test_the_chat_history_stays_intact_and_marks_what_was_forgotten():
    seed()
    forget.forget_values("me", [SECRET])
    shown = TestClient(app).get("/threads/A/messages").json()
    assert [m["text"] for m in shown] == [
        SECRET,
        f"Noted: {SECRET}",
        "Unrelated: I like tea.",
        "Good choice.",
    ]
    assert [m["forgotten"] for m in shown] == [True, True, False, False]
    assert [m.text for m in store.thread_messages("A")][0] == SECRET  # the stored row, as written


def test_a_forgotten_message_is_invisible_to_every_memory_path():
    original, record = seed()
    forget.forget_values("me", [SECRET])
    # replay to the model: a neutral placeholder pair, never the content
    replay = str(store.recent_history("A", budget=2500, step=8, chars_per_token=3))
    assert "873129" not in replay and store.FORGOTTEN_REPLY in replay
    assert "tea" in replay  # the rest of the conversation is replayed as usual
    # exact scan, keyword search (and after a repair pass), direct reads, neighbours
    assert not find("me", SECRET).total() and not find("me", "873129", session_id="A").total()
    assert not records.search_memory(CALLER, "Velvet Otter")
    search.reindex_missing()
    assert not records.search_memory(CALLER, "Velvet Otter 873129")
    with session() as s:  # no index row was (re)created for the forgotten message
        rowids = {r[0] for r in s.connection().exec_driver_sql("SELECT rowid FROM message_fts")}
    assert original.id not in rowids
    with pytest.raises(MemoryAccessError):
        records.read(CALLER, refs.msg(original.id))
    neighbour = store.thread_messages("A")[2]
    around = records.expand(CALLER, refs.msg(neighbour.id), before=5, after=5)
    assert all("873129" not in m.text for m in around)


def test_everything_derived_from_a_forgotten_message_is_truly_deleted():
    original, record = seed()
    report = forget.forget_values("me", [SECRET])
    with pytest.raises(MemoryAccessError):
        records.read(CALLER, record.ref)
    assert report.records == 1 and report.tool_outputs == 1 and report.messages == 2
    with session() as s:  # the only stored copy left is the chat message itself
        assert [m.id for m in s.exec(Message.__table__.select()).all() if SECRET in m.text]


def test_notes_are_deleted_and_never_rebuilt_from_forgotten_text():
    for i in range(60):
        core.ingest("me", "A", SECRET if i == 3 else f"filler {i}", reply="ok")
    core.flush("me", "A")
    with session() as s:
        assert s.exec(EpisodeNote.__table__.select()).all()
    forget.forget_values("me", [SECRET])
    with session() as s:
        assert not [n for n in s.exec(EpisodeNote.__table__.select()).all() if "873129" in n.text]
    rows = store.turns("A", [4])
    assert "873129" not in notes.segment_prompt(4, 4, rows, 2000)
    assert "[forgotten]" in notes.segment_prompt(4, 4, rows, 2000)


def test_the_reply_of_the_forgetting_turn_is_returned_as_written_and_stored_forgotten():
    core.ingest("me", "A", SECRET, reply="Noted")
    worker = worker_answering(
        Mode.FORGET,
        {"status": "erased", "summary": "Forgot the code."},
        steps=[("erase", {"values": [SECRET]})],
    )
    chat = build_model(
        get_settings(), raw=scripted([("forget", {"what": "my code"})], f"I forgot {SECRET}.")
    )
    with worker, get_agent().override(model=chat):
        reply = core.send("me", "A", "Forget my code.")
    assert reply == f"I forgot {SECRET}."
    last = TestClient(app).get("/threads/A/messages").json()[-1]
    assert last["text"] == reply and last["forgotten"] is True
    assert "873129" not in str(store.recent_history("A", budget=2500, step=8, chars_per_token=3))


def test_a_turn_can_be_forgotten_as_a_whole_by_message_ref():
    core.ingest("me", "A", "We are staying at the blue house by the harbour.", reply="Nice view!")
    target = store.thread_messages("A")[0]
    forget.forget_values("me", [], message_ids=[target.id])
    shown = TestClient(app).get("/threads/A/messages").json()
    assert [(m["text"], m["forgotten"]) for m in shown] == [
        ("We are staying at the blue house by the harbour.", True),
        ("Nice view!", True),
    ]
    assert not find("me", "harbour").total()


def test_hard_mode_still_deletes_the_messages_themselves():
    seed()
    forget.forget_values("me", [SECRET], mode="hard")
    assert SECRET not in TestClient(app).get("/threads/A/messages").text


def test_existing_databases_get_the_flag_column(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE message (id INTEGER PRIMARY KEY, text TEXT)")
        conn.exec_driver_sql("INSERT INTO message (text) VALUES ('kept')")
    _add_missing_columns(engine)
    _add_missing_columns(engine)  # idempotent
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT text, forgotten FROM message").all() == [("kept", 0)]


# --- forgetting a whole session: deterministic, no value to search for ----------------------


def seed_two_sessions():
    for text in ("We are staying at the blue house.", "Dana is my manager."):
        core.ingest("me", "A", text, reply="Noted.")
    core.ingest("me", "B", "Unrelated: I like tea.", reply="Good choice.")
    a_first = store.thread_messages("A")[0]
    b_first = store.thread_messages("B")[0]
    a, b = MemoryCaller("me", "A"), MemoryCaller("me", "B")
    made = {
        "a_session": records.write(
            a,
            store="session",
            kind="fact",
            key="stay",
            text="Blue house",
            sources=[refs.msg(a_first.id)],
        ),
        "b_session": records.write(
            b,
            store="session",
            kind="fact",
            key="drink",
            text="Tea",
            sources=[refs.msg(b_first.id)],
        ),
        "a_only": records.write(
            a,
            store="long_term",
            kind="fact",
            key="manager",
            text="Dana",
            sources=[refs.msg(a_first.id)],
        ),
        "b_only": records.write(
            b,
            store="long_term",
            kind="fact",
            key="tea",
            text="Likes tea",
            sources=[refs.msg(b_first.id)],
        ),
        "a_out": records.save_tool_output(a, "call-a", "ask_memory", "answer from A"),
        "b_out": records.save_tool_output(b, "call-b", "ask_memory", "answer from B"),
    }
    return {k: getattr(v, "ref", v) for k, v in made.items()}  # records are views, outputs refs


def test_forgetting_a_session_flags_all_its_messages_and_nothing_else():
    made = seed_two_sessions()
    report = forget.forget_session("me", "A")
    shown = {t: [m.forgotten for m in store.thread_messages(t)] for t in ("A", "B")}
    assert shown == {"A": [True] * 4, "B": [False, False]}
    assert report.messages == 4 and report.records == 2 and report.tool_outputs == 1
    for gone in ("a_session", "a_only", "a_out"):
        with pytest.raises(MemoryAccessError):
            records.read(MemoryCaller("me", "A"), made[gone])
    for kept in ("b_session", "b_only", "b_out"):
        records.read(MemoryCaller("me", "B"), made[kept])


def test_forgetting_a_session_keeps_the_turn_that_asked_for_it():
    core.ingest("me", "A", "My locker code is 5-17-30.", reply="Noted.")
    core.ingest("me", "A", "Forget everything I said in this session.")
    request = store.thread_messages("A")[-1]
    # a session record written while answering the request cites it, so only scope can remove it
    note = records.write(
        CALLER,
        store="session",
        kind="fact",
        key="locker",
        text="Locker code",
        sources=[refs.msg(request.id)],
    )
    report = forget.forget_session("me", "A", keep_message_id=request.id)
    assert report.messages == 2 and report.records == 1
    with pytest.raises(MemoryAccessError):
        records.read(CALLER, note.ref)
    assert [m.forgotten for m in store.thread_messages("A")] == [True, True, False]


def test_forgetting_an_empty_session_does_nothing():
    assert forget.forget_session("me", "nope").total() == 0


def test_the_model_forgets_a_whole_session_with_one_tool_call_and_no_worker_run():
    seed_two_sessions()
    chat = build_model(get_settings(), raw=scripted([("forget_session", {})], "Done."))
    with get_agent().override(model=chat):  # no worker override: the memory worker is not used
        reply = core.send("me", "A", "Forget everything I said in this session.")
    assert reply == "Done."
    forgotten = [m.forgotten for m in store.thread_messages("A")]
    assert forgotten == [True] * 4 + [False, False]  # earlier turns gone; request and reply kept
    assert [m.forgotten for m in store.thread_messages("B")] == [False, False]
