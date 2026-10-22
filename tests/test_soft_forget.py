"""Display-only forgotten messages must never return through model memory paths."""

import pytest
from fastapi.testclient import TestClient

from chatbot import core
from chatbot.agent.agent import get_agent
from chatbot.agent.models import build_model
from chatbot.capabilities.memory.subagent import Mode
from chatbot.config import get_settings
from chatbot.memory import forget, records, refs, search
from chatbot.memory import messages as store
from chatbot.memory.grep import find
from chatbot.memory.records import MemoryAccessError, MemoryCaller
from chatbot.surfaces.http import app
from tests.fakes import scripted, worker_answering
from tests.test_forget import on_disk


def test_soft_forget_preserves_display_but_blocks_all_memory_paths():
    secret = "Velvet Otter access code 873129"
    core.ingest("me", "A", secret, reply=f"Noted: {secret}")
    caller = MemoryCaller("me", "A")
    original = store.thread_messages("A")[0]
    record = records.write(
        caller,
        store="long_term",
        kind="fact",
        key="code",
        text=secret,
        sources=[refs.msg(original.id)],
    )
    forget.forget_values("me", [secret])
    assert secret not in str(store.recent_history("A", budget=2500, step=8, chars_per_token=3))
    assert not find("me", secret).total()
    assert not records.search_memory(caller, "Velvet Otter")
    with pytest.raises(MemoryAccessError):
        records.read(caller, record.ref)
    assert secret not in str(records.read(caller, refs.msg(original.id)))
    search.reindex_missing()
    assert not records.search_memory(caller, "Velvet Otter")
    client = TestClient(app)
    assert [m["text"] for m in client.get("/threads/A/messages").json()] == [
        "[forgotten]",  # the conversation stays visible, the forgotten value does not
        "Noted: [forgotten]",
    ]
    core.ingest("other", "B", "unrelated", reply="ok")
    assert client.get("/threads/B/messages").status_code == 404
    forget.forget_values("me", [secret], mode="hard")
    assert secret not in client.get("/threads/A/messages").text


def test_soft_forget_returns_the_current_reply_as_written_but_keeps_it_only_masked():
    secret = "Velvet Otter access code 873129"
    core.ingest("me", "A", secret, reply="Noted")
    with (
        worker_answering(
            Mode.FORGET,
            {"status": "erased", "summary": "Forgot the code."},
            steps=[("erase", {"values": [secret]})],
        ),
        get_agent().override(
            model=build_model(
                get_settings(),
                raw=scripted([("forget", {"what": "my code"})], f"I forgot {secret}."),
            )
        ),
    ):
        reply = core.send("me", "A", "Forget my code.")
    assert reply == f"I forgot {secret}."
    assert secret not in str(store.recent_history("A", budget=2500, step=8, chars_per_token=3))
    shown = TestClient(app).get("/threads/A/messages").json()[-1]["text"]
    assert shown == "I forgot [forgotten]."  # what stays visible and stored is masked
    assert not on_disk("873129")


def test_soft_forget_keeps_the_conversation_readable_with_the_value_masked_and_off_disk():
    core.ingest("me", "A", "My address is 27 Larkspur Lane, Apt 4B for the form.", reply="Thanks!")
    forget.forget_values("me", ["27 Larkspur Lane, Apt 4B"])  # default mode: soft
    shown = [m["text"] for m in TestClient(app).get("/threads/A/messages").json()]
    assert shown == ["My address is [forgotten] for the form.", "Thanks!"]
    assert not on_disk("Larkspur") and not on_disk("Apt 4B")
    assert "Larkspur" not in str(store.recent_history("A", budget=2500, step=8, chars_per_token=3))


def test_a_later_forget_also_masks_values_in_earlier_display_copies():
    core.ingest("me", "A", "Gym code 111-222 at Cobalt Finch Hall.", reply="ok")
    forget.forget_values("me", ["111-222"])
    client = TestClient(app)
    assert client.get("/threads/A/messages").json()[0]["text"] == (
        "Gym code [forgotten] at Cobalt Finch Hall."
    )
    forget.forget_values("me", ["Cobalt Finch Hall"])  # nothing active mentions it any more
    assert client.get("/threads/A/messages").json()[0]["text"] == (
        "Gym code [forgotten] at [forgotten]."
    )
    assert not on_disk("Cobalt") and not on_disk("111-222")


def test_a_turn_forgotten_as_a_whole_shows_a_bare_note():
    core.ingest("me", "A", "We are staying at the blue house by the harbour.", reply="Nice view!")
    target = store.thread_messages("A")[0]
    forget.forget_values("me", [], message_ids=[target.id])
    shown = [m["text"] for m in TestClient(app).get("/threads/A/messages").json()]
    assert shown == [forget.DISPLAY_NOTE, forget.DISPLAY_NOTE]
    assert not on_disk("harbour")
