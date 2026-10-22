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
        secret,
        f"Noted: {secret}",
    ]
    core.ingest("other", "B", "unrelated", reply="ok")
    assert client.get("/threads/B/messages").status_code == 404
    forget.forget_values("me", [secret], mode="hard")
    assert secret not in client.get("/threads/A/messages").text


def test_soft_forget_does_not_redact_current_reply_but_excludes_it_from_replay():
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
    assert TestClient(app).get("/threads/A/messages").json()[-1]["text"] == reply
