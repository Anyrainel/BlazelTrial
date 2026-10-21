"""PLAN sequence step 5: forget is a hard delete everywhere (invariant 7, contract §7)."""

from datetime import datetime

from chatbot import core
from chatbot.agent.agent import get_agent
from chatbot.agent.models import build_model
from chatbot.capabilities.messaging import sent_messages
from chatbot.config import get_settings
from chatbot.memory import messages as store
from chatbot.memory import records, refs
from chatbot.memory.db import get_engine, session
from chatbot.memory.forget import forget_values
from chatbot.memory.models import EpisodeNote, Fact, FactKind, MemoryRecord, OutboundMessage, Scope
from chatbot.memory.records import MemoryCaller
from scenarios.fixtures.slack_history import generate
from tests.fakes import scripted, search_agent_answering

DM = MemoryCaller("me", "slack-dm")
A = MemoryCaller("me", "A")
ADDRESS = "27 Larkspur Lane, Apt 4B, Portland, OR 97214"


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

    # The turn: look it up, forget it, and (badly) echo it in the reply.
    answer = {
        "status": "found",
        "answer": ADDRESS,
        "sources": [{"ref": address_msg(), "excerpt": ADDRESS}],
    }
    steps = [
        ("ask_memory", {"question": "user's home address"}),
        ("forget", {"values": [ADDRESS, "Larkspur Lane"]}),
    ]
    with (
        search_agent_answering(answer, steps=[("search", {"query": "address"})]),
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


def test_short_or_generic_values_are_refused():
    core.ingest("me", "A", "I live in Oregon, OR.", reply="ok")
    report = forget_values("me", ["OR", "  ", "Ore"])
    assert report.total() == 0 and len(report.rejected) == 3
    assert store.thread_messages("A")  # nothing deleted


def test_forget_is_scoped_to_the_user():
    core.ingest("me", "A", f"Mine: {ADDRESS}", reply="ok")
    core.ingest("you", "Z", f"Theirs: {ADDRESS}", reply="ok")
    forget_values("me", [ADDRESS])
    assert [m.text for m in store.thread_messages("Z")][0].startswith("Theirs")
    with session() as s:
        assert s.exec(MemoryRecord.__table__.select()).all() == []
