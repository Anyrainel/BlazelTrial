"""PLAN sequence step 5: forget is a hard delete everywhere (invariant 7, contract §7)."""

from datetime import datetime

import pytest

from chatbot import core
from chatbot.agent.agent import get_agent
from chatbot.agent.models import build_model
from chatbot.capabilities.memory import subagent
from chatbot.capabilities.memory.subagent import Mode
from chatbot.capabilities.messaging import sent_messages
from chatbot.config import get_settings
from chatbot.memory import messages as store
from chatbot.memory import records, refs
from chatbot.memory.db import get_engine, session
from chatbot.memory.forget import forget_values
from chatbot.memory.models import (
    EpisodeNote,
    Fact,
    FactKind,
    GeneratedPost,
    MemoryRecord,
    OutboundMessage,
    Scope,
)
from chatbot.memory.records import MemoryCaller
from scenarios.fixtures.slack_history import generate
from tests.fakes import scripted, worker_answering

DM = MemoryCaller("me", "slack-dm")
A = MemoryCaller("me", "A")
ADDRESS = "27 Larkspur Lane, Apt 4B, Portland, OR 97214"


@pytest.fixture(autouse=True)
def hard_forget_contract(monkeypatch):
    """These regressions explicitly verify permanent deletion, including disk bytes."""
    monkeypatch.setenv("MEMORY_FORGET_MODE", "hard")
    get_settings.cache_clear()


def on_disk(needle: str) -> bool:
    """Is `needle` anywhere in the database files (db + WAL), case-insensitively?"""
    get_engine().dispose()  # close pooled connections so files are complete
    path = get_settings().db_path
    blob = b""
    for p in (path, path.with_name(path.name + "-wal")):
        if p.exists():
            blob += p.read_bytes()
    return needle.lower().encode() in blob.lower()


def load_history() -> None:
    for row in generate(120):
        core.ingest(
            "me", "slack-dm", row["user"], row["assistant"], datetime.fromisoformat(row["ts"])
        )
    core.flush("me", "slack-dm")


def address_msg() -> str:
    return refs.msg(next(m.id for m in store.thread_messages("slack-dm") if "Larkspur" in m.text))


def seed_derived_state() -> dict:
    src = address_msg()
    rec = records.write(
        DM,
        store="long_term",
        kind="fact",
        key="home_address",
        text=f"The user lives at {ADDRESS}.",
        sources=[src],
    )
    moved = records.write(
        DM,
        store="long_term",
        kind="fact",
        key="moved",
        text="The user moved house recently.",
        sources=[src],
    )  # derived only
    partner_src = refs.msg(
        next(m.id for m in store.thread_messages("slack-dm") if "partner Sam" in m.text)
    )
    partner = records.write(
        DM,
        store="long_term",
        kind="fact",
        key="partner",
        text="The user's partner is Sam.",
        sources=[partner_src, src],
    )  # mixed
    core.ingest("me", "A", f"My address is {ADDRESS} for the delivery form.", reply="ok")
    records.save_tool_output(DM, "call-x", "ask_memory", f"answer: {ADDRESS}")
    records.save_checkpoint(DM, "run-x", f"Goal: send address {ADDRESS}")
    with session() as s:
        s.add(OutboundMessage(thread_id="slack-dm", recipient="Sam", text=f"I'm at {ADDRESS}"))
        s.add(GeneratedPost(thread_id="slack-dm", text=f"Visit us at {ADDRESS}! #Launch"))
        s.add(
            Fact(
                user_id="me",
                scope=Scope.USER,
                key="home_address",
                text=f"Lives at {ADDRESS}.",
                kind=FactKind.FACT,
                source_thread_id="slack-dm",
                source_turn=100,
            )
        )
        s.commit()
    return {"rec": rec.ref, "moved": moved.ref, "partner": partner.ref}


def test_forget_removes_the_value_everywhere_including_disk():
    load_history()
    made = seed_derived_state()
    assert on_disk("Larkspur")
    with session() as s:
        notes_before = len(s.exec(EpisodeNote.__table__.select()).all())

    # The turn: the worker finds how it was written, erases it, and the chat model (badly)
    # echoes the address in its reply.
    worker_steps = [
        ("grep", {"text": "Larkspur"}),
        ("erase", {"values": [ADDRESS, "Larkspur Lane"]}),
    ]
    done = {"status": "erased", "summary": "Removed the address everywhere."}
    steps = [("forget", {"what": "my home address"})]
    with (
        worker_answering(Mode.FORGET, done, steps=worker_steps),
        get_agent().override(
            model=build_model(get_settings(), raw=scripted(steps, f"Done, I forgot {ADDRESS}."))
        ),
    ):
        reply = core.send("me", "slack-dm", "Forget my address.")

    assert "Larkspur" not in reply and "[forgotten]" in reply
    assert not on_disk("Larkspur")  # messages, runs, records, excerpts, notes, outputs, FTS...
    assert not on_disk("97214")
    for ref in (made["rec"], made["moved"]):
        with __import__("pytest").raises(records.MemoryAccessError):
            records.read(DM, ref)
    partner = records.read(DM, made["partner"])
    assert partner.current and len(partner.sources) == 1  # kept, lost only the forgotten source
    for caller in (DM, A):
        hits = records.search_memory(caller, "Larkspur address Portland 97214")
        assert not any("Larkspur" in h.excerpt or "97214" in h.excerpt for h in hits)
    with session() as s:
        # The note over the forgotten turn was deleted, then rebuilt from the remaining messages.
        assert len(s.exec(EpisodeNote.__table__.select()).all()) == notes_before
        assert s.exec(OutboundMessage.__table__.select()).all() == []
        assert s.exec(GeneratedPost.__table__.select()).all() == []


def test_a_message_after_forgetting_cannot_leak_the_address():
    load_history()
    forget_values("me", [ADDRESS])
    steps = [
        ("send_message", {"to": "Sam", "text": "Here's where I live: (asked the user to confirm)"})
    ]
    with get_agent().override(model=build_model(get_settings(), raw=scripted(steps, "Sent."))):
        core.send("me", "slack-dm", "Message my partner where I live.")
    assert all("Larkspur" not in m.text for m in sent_messages())
    assert not on_disk("Larkspur")


def test_forgetting_a_version_keeps_the_chain_consistent():
    core.ingest("me", "A", f"I live at {ADDRESS}.", reply="ok")
    core.ingest("me", "A", "We moved to 5 Oak Street.", reply="ok")
    m1, m2 = [refs.msg(m.id) for m in store.thread_messages("A") if m.role == "user"]
    v1 = records.write(
        A, store="long_term", kind="fact", key="home", text=f"Lives at {ADDRESS}.", sources=[m1]
    )
    v2 = records.write(
        A,
        store="long_term",
        kind="fact",
        key="home",
        text="Lives at 5 Oak Street.",
        sources=[m2],
        supersedes=v1.ref,
    )
    forget_values("me", ["Larkspur Lane"])
    view = records.read(A, v2.ref)
    assert view.current and view.supersedes is None
    assert [v.ref for v in records.versions(A, v2.ref)] == [v2.ref]


def test_short_values_can_be_forgotten_but_empty_values_are_invalid():
    core.ingest("me", "A", "My colleague is Xi.", reply="ok")
    report = forget_values("me", ["Xi", "  "])
    assert report.messages == 1 and report.rejected == [""]
    assert all("Xi" not in m.text for m in store.thread_messages("A"))


def test_forget_is_scoped_to_the_user():
    core.ingest("me", "A", f"Mine: {ADDRESS}", reply="ok")
    core.ingest("you", "Z", f"Theirs: {ADDRESS}", reply="ok")
    forget_values("me", [ADDRESS])
    assert [m.text for m in store.thread_messages("Z")][0].startswith("Theirs")
    with session() as s:
        assert s.exec(MemoryRecord.__table__.select()).all() == []


def test_forget_keeps_fulfilled_draft_completed_in_replay():
    from pydantic_ai.messages import ModelRequest, ModelResponse

    from chatbot.memory.models import Role

    store.ensure_thread("me", "slack-CONE")
    store.record_turn(
        "me",
        "slack-CONE",
        "For this draft only use LOCAL-ONE",
        "LOCAL-ONE: Cobalt Finch launches today",
    )
    forget_values("me", ["Cobalt Finch"])
    rows = store.thread_messages("slack-CONE")
    assert [r.role for r in rows] == [Role.USER, Role.ASSISTANT]
    history = store._turn_messages(rows)
    assert isinstance(history[0], ModelRequest)
    assert isinstance(history[1], ModelResponse)
    assert "completed" in history[1].parts[0].content
    assert all("Cobalt Finch" not in r.text for r in rows)
    assert not on_disk("Cobalt Finch")


# --- the forget worker: finding, guarding and erasing intelligently -------------------------------


def run_forget(steps: list, seen: list | None = None, final: dict | None = None):
    final = final or {"status": "erased", "summary": "done"}
    with worker_answering(Mode.FORGET, final, steps=steps, seen=seen):
        return core._run_sync(subagent.work(Mode.FORGET, DM, "Forget: my address"))


def test_grep_finds_exact_strings_everywhere_when_forgetting_but_only_the_session_otherwise():
    core.ingest("me", "A", "Locker code is 55-81-23.", reply="ok")
    core.ingest("me", "B", "Gym locker is 55-81-23 too.", reply="ok")
    seen: list[str] = []
    with worker_answering(
        Mode.READ, {"status": "not_found", "answer": "-"}, [("grep", {"text": "55-81-23"})], seen
    ):
        core._run_sync(subagent.ask(A, "locker?"))
    assert "Matches: 1 message" in seen[0]  # session A only
    seen.clear()
    run_forget([("grep", {"text": "55 81 23".replace(" ", "-")})], seen)
    assert "Matches: 2 message" in seen[0]  # every conversation


def test_erase_refuses_a_string_that_matches_too_many_messages(monkeypatch):
    monkeypatch.setenv("MEMORY_ERASE_MAX_MESSAGES", "2")
    get_settings.cache_clear()
    for i in range(4):
        core.ingest("me", "A", f"Sam picked up the kids, round {i}.", reply="ok")
    seen: list[str] = []
    outcome, _ = run_forget([("erase", {"values": ["picked up the kids"]})], seen)
    assert seen[0].startswith("Nothing erased") and "matches 4 messages" in seen[0]
    assert len(store.thread_messages("A")) == 8
    seen.clear()
    run_forget([("erase", {"values": ["picked up the kids"], "many_ok": True})], seen)
    assert [m.text for m in store.thread_messages("A")] == ["ok"] * 4  # only the replies


def test_erase_by_message_ref_deletes_the_whole_turn_and_purges_the_bytes():
    core.ingest("me", "A", "We are staying at the blue house by the harbour.", reply="Nice view!")
    core.ingest("me", "A", "Unrelated: I like tea.", reply="ok")
    target = next(m for m in store.thread_messages("A") if "harbour" in m.text)
    outcome, deps = run_forget([("erase", {"values": [], "messages": [refs.msg(target.id)]})])
    texts = [m.text for m in store.thread_messages("A")]
    assert texts == ["Unrelated: I like tea.", "ok"]  # the reply to the turn went too
    assert deps.erased_refs and deps.erased == []


def test_forget_tool_purges_disk_after_a_value_less_erase():
    core.ingest("me", "A", "We are staying at the blue house by the harbour.", reply="Nice view!")
    target = next(m for m in store.thread_messages("A") if "harbour" in m.text)
    steps = [("forget", {"what": "where we are staying"})]
    erase = [("erase", {"messages": [refs.msg(target.id)], "values": []})]
    done = {"status": "erased", "summary": "Removed the stay."}
    with (
        worker_answering(Mode.FORGET, done, steps=erase),
        get_agent().override(model=build_model(get_settings(), raw=scripted(steps, "Done."))),
    ):
        core.send("me", "A", "Forget where we're staying.")
    assert not on_disk("harbour")


def test_erase_reports_when_a_value_is_still_found_and_never_echoes_it_in_the_summary():
    core.ingest("me", "A", "My locker code is 55-81-23.", reply="ok")
    outcome, deps = run_forget(
        [("erase", {"values": ["55-81-23"]})],
        final={"status": "erased", "summary": "Erased code 55-81-23."},
    )
    assert "55-81-23" not in outcome.summary and "[forgotten]" in outcome.summary
    assert deps.erased == ["55-81-23"]


def test_forget_tool_purges_disk_when_the_run_never_carried_the_value():
    core.ingest("me", "A", "My locker code is 55-81-23.", reply="Noted.")
    steps = [("forget", {"what": "my locker code"})]
    done = {"status": "erased", "summary": "Removed the locker code."}
    with (
        worker_answering(Mode.FORGET, done, steps=[("erase", {"values": ["55-81-23"]})]),
        get_agent().override(model=build_model(get_settings(), raw=scripted(steps, "Done."))),
    ):
        core.send("me", "A", "Forget my locker code.")
    assert not on_disk("55-81-23")
