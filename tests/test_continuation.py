"""PLAN sequence step 4: long current runs, no duplicate mutations, maintenance recovery,
document passages in memory search."""

import json

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from chatbot import core
from chatbot.agent.agent import get_agent
from chatbot.agent.budget import estimate_tokens
from chatbot.agent.models import build_model
from chatbot.capabilities.memory.subagent import Mode
from chatbot.capabilities.messaging import sent_messages
from chatbot.config import get_settings
from chatbot.memory import records
from chatbot.memory.db import session
from chatbot.memory.models import EpisodeNote, MemoryRecord
from chatbot.memory.notes import get_note_writer
from chatbot.memory.records import MemoryCaller
from tests.fakes import scripted, worker_answering

A = MemoryCaller("me", "A")


# --- long current run ---------------------------------------------------------------------------


def test_a_long_tool_loop_is_compacted_into_saved_outputs_and_a_checkpoint(monkeypatch):
    monkeypatch.setenv("TOKEN_COUNT_MODE", "estimate")
    get_settings.cache_clear()
    monkeypatch.setattr(
        "chatbot.capabilities.memory.subagent.render",
        lambda answer: "evidence block " + "w" * 4000,
    )
    sizes: list[int] = []
    calls = 0

    def fn(messages, info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        from pydantic_ai.models import ModelRequestParameters

        sizes.append(estimate_tokens(list(messages), ModelRequestParameters(), 3.0))
        if calls <= 8:  # 8 big results in one run: ~11k tokens if nothing were compacted
            return ModelResponse(parts=[ToolCallPart("ask_memory", {"question": f"q{calls}"})])
        return ModelResponse(parts=[TextPart("done")])

    with get_agent().override(model=build_model(get_settings(), raw=FunctionModel(fn))):
        assert core.send("me", "A", "Research everything.") == "done"

    target = get_settings().context_target_tokens
    assert max(sizes) <= target  # every request of the run stayed under target
    run = json.loads(
        [m for m in __import__("chatbot.memory.messages", fromlist=["x"]).thread_messages("A")][
            -1
        ].run_messages_json
    )
    returns = [p for m in run for p in m["parts"] if p.get("part_kind") == "tool-return"]
    calls_ = [p for m in run for p in m["parts"] if p.get("part_kind") == "tool-call"]
    assert len(returns) == len(calls_) == 8  # pairs intact
    stubs = [r for r in returns if str(r["content"]).startswith("[saved as out:")]
    assert stubs and str(returns[-1]["content"]).startswith("evidence block")  # latest inline
    out_ref = str(stubs[0]["content"]).split()[2].rstrip(";")
    assert records.read(A, out_ref).text.startswith("evidence block")
    with session() as s:
        from chatbot.memory.models import Checkpoint

        [ckpt] = s.exec(Checkpoint.__table__.select()).all()
    assert "Goal: Research everything." in ckpt.text and out_ref in ckpt.text


def test_saved_outputs_are_session_private():
    ref = records.save_tool_output(A, "call-1", "ask_memory", "secret result")
    assert records.save_tool_output(A, "call-1", "ask_memory", "different") == ref  # idempotent
    with pytest.raises(records.MemoryAccessError):
        records.read(MemoryCaller("me", "B"), ref)


# --- no duplicate mutations ---------------------------------------------------------------------


def test_repeated_writes_and_sends_in_a_turn_happen_once():
    args = {
        "store": "long_term",
        "kind": "fact",
        "key": "partner",
        "text": "The user's partner is Sam.",
    }
    msg = {"to": "Sam", "text": "Home late tonight."}
    change = {"change": "The user's partner is Sam."}
    steps = [
        ("update_memory", change),
        ("update_memory", change),
        ("send_message", msg),
        ("send_message", msg),
    ]
    seen: list[str] = []
    saved = worker_answering(
        Mode.WRITE, {"status": "saved", "summary": "ok"}, steps=[("save", args)]
    )
    with (
        saved,
        get_agent().override(model=build_model(get_settings(), raw=scripted(steps, "ok", seen))),
    ):
        core.send("me", "A", "Remember my partner is Sam and tell him I'm home late.")
    with session() as s:
        assert len(s.exec(MemoryRecord.__table__.select()).all()) == 1
    assert len(sent_messages("A")) == 1
    assert seen[0] == seen[1]  # the repeat returns the original record
    assert "already recorded" in seen[3]


# --- maintenance recovery -----------------------------------------------------------------------


def _ingest(n: int, thread: str = "slack-dm") -> None:
    for i in range(n):
        core.ingest("me", thread, f"message {i}", reply="ok")


def test_note_maintenance_recovers_after_a_failure():
    settings = get_settings()
    calls = 0

    def flaky(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("note writer down")
        return ModelResponse(parts=[TextPart("note text")])

    with get_note_writer().override(model=build_model(settings, raw=FunctionModel(flaky))):
        _ingest(settings.summary_chunk_turns + settings.summary_keep_recent_turns)
        with session() as s:
            assert (
                s.exec(EpisodeNote.__table__.select()).all() == []
            )  # failed, nothing half-written
        core.ingest("me", "slack-dm", "one more", reply="ok")  # next upkeep retries
    with session() as s:
        notes = s.exec(EpisodeNote.__table__.select()).all()
    assert len(notes) == 1


def test_note_maintenance_resumes_after_a_restart():
    from tests.conftest import _clear_caches

    settings = get_settings()
    _ingest(settings.summary_chunk_turns + settings.summary_keep_recent_turns - 1)
    _clear_caches()  # "restart": new engine, fresh agents, same database file
    from chatbot.memory.notes import get_note_writer as fresh_writer

    with fresh_writer().override(
        model=build_model(
            get_settings(), raw=FunctionModel(lambda m, i: ModelResponse(parts=[TextPart("n")]))
        )
    ):
        core.ingest("me", "slack-dm", "after restart", reply="ok")
        core.flush("me", "slack-dm")
    with session() as s:
        assert len(s.exec(EpisodeNote.__table__.select()).all()) == 1


# --- documents in memory search -----------------------------------------------------------------


def test_opened_document_passages_are_searchable_from_that_session_only():
    from chatbot.capabilities.docs import remember_ref
    from chatbot.docs_reader import reader
    from scenarios.fixtures.docs import Q3_LAUNCH_DATE, write_all

    write_all(get_settings().docs_path)
    reader.ensure_indexed(reader.load("Q3-plan"))
    assert not [h for h in records.search_memory(A, "public launch date") if h.type == "passage"]
    remember_ref("A", "Q3-plan")
    hits = [h for h in records.search_memory(A, "public launch date Lumen") if h.type == "passage"]
    assert hits and any(Q3_LAUNCH_DATE in records.read(A, h.ref).text for h in hits)
    assert not [
        h
        for h in records.search_memory(MemoryCaller("me", "B"), "public launch date")
        if h.type == "passage"
    ]


def test_tool_side_effects_survive_a_retried_turn(monkeypatch):
    """A pre-flight refusal mid-turn restarts the run; what the first attempt's tools recorded
    (here: values forgotten) must still drive the post-turn scrub."""
    from chatbot.agent.budget import BudgetExceededError

    core.ingest("me", "A", "My locker code is 55-81-23.", reply="ok")
    attempts = 0

    def fn(messages, info: AgentInfo) -> ModelResponse:
        nonlocal attempts
        done = any(
            isinstance(p, ToolReturnPart)
            for m in messages
            if isinstance(m, ModelRequest)
            for p in m.parts
        )
        if not done:
            attempts += 1
            if attempts == 1:
                return ModelResponse(parts=[ToolCallPart("forget", {"what": "my locker code"})])
        if attempts == 1:  # first attempt: refused after its tool already ran
            raise BudgetExceededError(9999, 8000, "preflight")
        return ModelResponse(parts=[TextPart("Forgot 55-81-23.")])

    erased = worker_answering(
        Mode.FORGET,
        {"status": "erased", "summary": "Removed the locker code."},
        steps=[("erase", {"values": ["55-81-23"]})],
    )
    with (
        erased,
        get_agent().override(model=build_model(get_settings(), raw=FunctionModel(fn))),
    ):
        reply = core.send("me", "A", "Forget my locker code.")
    assert attempts == 2 and "55-81-23" not in reply
