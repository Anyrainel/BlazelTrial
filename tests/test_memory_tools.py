"""PLAN sequence step 3: ask_memory subagent, explicit write tools, no harness injection."""

import json

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from chatbot import core
from chatbot.agent.agent import get_agent
from chatbot.agent.models import build_model
from chatbot.capabilities.memory import subagent
from chatbot.config import get_settings
from chatbot.memory import messages as store
from chatbot.memory import records
from chatbot.memory.records import MemoryCaller
from tests.fakes import prompt_text, scripted, search_agent_answering

A = MemoryCaller("me", "A")
B = MemoryCaller("me", "B")


def chat(steps: list, final: str = "ok", seen: list | None = None):
    return get_agent().override(model=build_model(get_settings(), raw=scripted(steps, final, seen)))


def remember_args(**kw) -> dict:
    return {
        "store": "long_term",
        "kind": "fact",
        "key": "manager",
        "text": "The user's manager is Priya Raman.",
        **kw,
    }


# --- writes -------------------------------------------------------------------------------------


def test_remember_cites_the_current_message_and_is_shared_across_sessions():
    with chat([("remember", remember_args())]):
        core.send("me", "A", "My manager is Priya Raman.")
    [hit] = records.search_memory(B, "manager", store="long_term")
    view = records.read(B, hit.ref)
    assert view.text == "The user's manager is Priya Raman."
    current = next(m for m in store.thread_messages("A") if m.text == "My manager is Priya Raman.")
    assert [s.ref for s in view.sources] == [f"msg:{current.id}"]  # stored before the run


def test_session_store_stays_in_its_session():
    args = remember_args(
        store="session", kind="preference", key="tone", text="Keep replies terse here."
    )
    with chat([("remember", args)]):
        core.send("me", "A", "In this chat keep replies terse.")
    assert records.search_memory(A, "terse", store="session")
    assert records.search_memory(B, "terse") == []


def test_update_memory_keeps_the_old_version_as_history():
    pref = remember_args(kind="preference", key="spelling", text="Use British spelling.")
    with chat([("remember", pref)]):
        core.send("me", "A", "Always use British spelling.")
    [old] = records.search_memory(A, "spelling", store="long_term")
    with chat([("update_memory", {"record": old.ref, "text": "Use American spelling."})]):
        core.send("me", "A", "Switch to American spelling.")
    chain = records.versions(B, old.ref)
    assert [v.text for v in chain] == ["Use British spelling.", "Use American spelling."]
    assert [v.current for v in chain] == [False, True]


def test_write_errors_come_back_to_the_model_not_as_crashes():
    seen: list[str] = []
    with chat([("update_memory", {"record": "rec:999", "text": "x"})], seen=seen):
        core.send("me", "A", "change it")
    assert seen and seen[0].startswith("Not saved: unknown or not visible: rec:999")


# --- ask_memory ---------------------------------------------------------------------------------


def _remember_manager() -> str:
    with chat([("remember", remember_args())]):
        core.send("me", "A", "My manager is Priya Raman.")
    return records.search_memory(A, "manager", store="long_term")[0].ref


def test_ask_memory_returns_status_answer_and_verified_sources():
    ref = _remember_manager()
    final = {
        "status": "found",
        "answer": "Priya Raman.",
        "sources": [{"ref": ref, "excerpt": "manager is Priya Raman"}],
    }
    seen: list[str] = []
    with (
        search_agent_answering(final, steps=[("search", {"query": "manager"})]),
        chat([("ask_memory", {"question": "Who is the user's manager?"})], seen=seen),
    ):
        core.send("me", "B", "Who's my manager?")
    assert seen[0].startswith("status: found")
    assert "Priya Raman" in seen[0] and ref in seen[0]


def test_unseen_sources_are_dropped_and_found_is_downgraded():
    final = {
        "status": "found",
        "answer": "Bob.",
        "sources": [{"ref": "rec:777", "excerpt": "made up"}],
    }
    answer = None
    with search_agent_answering(final):
        answer = core._run_sync(subagent.ask(A, "Who is my manager?"))
    assert answer.status == "incomplete" and answer.sources == []
    assert "verified" in answer.caveats


def test_subagent_only_sees_the_callers_memory():
    with chat(
        [("remember", remember_args(store="session", key="secret", text="Vault code is 4412."))]
    ):
        core.send("me", "A", "The vault code is 4412.")
    [secret] = [h for h in records.search_memory(A, "vault", store="session") if h.type == "record"]
    seen: list[str] = []
    with search_agent_answering(
        {"status": "not_found", "answer": "-"}, steps=[("read", {"ref": secret.ref})], seen=seen
    ):
        core._run_sync(subagent.ask(B, "vault code?"))
    assert seen == [f"Error: unknown or not visible: {secret.ref}"]


def test_subagent_step_limit_yields_incomplete(monkeypatch):
    monkeypatch.setenv("MEMORY_SEARCH_MAX_REQUESTS", "3")
    get_settings.cache_clear()
    endless = [("search", {"query": f"q{i}"}) for i in range(10)]
    with search_agent_answering({"status": "found", "answer": "x"}, steps=endless):
        answer = core._run_sync(subagent.ask(A, "anything"))
    assert answer.status == "incomplete" and "ran out of steps" in answer.answer


def test_subagent_tool_output_budget_is_enforced(monkeypatch):
    monkeypatch.setenv("MEMORY_SEARCH_TOOL_CHARS", "300")
    get_settings.cache_clear()
    for i in range(6):
        core.ingest("me", "A", f"Saffron Table note {i}. " + "detail " * 40, reply="ok")
    seen: list[str] = []
    steps = [("search", {"query": "Saffron"}), ("search", {"query": "Saffron Table"})]
    with search_agent_answering({"status": "found", "answer": "x"}, steps=steps, seen=seen):
        core._run_sync(subagent.ask(A, "Saffron?"))
    assert len(seen[0]) <= 300
    assert seen[1].startswith("Tool budget used up")


def test_memory_search_calls_are_tagged_in_the_call_log():
    with search_agent_answering(
        {"status": "not_found", "answer": "-"}, steps=[("search", {"query": "x"})]
    ):
        with chat([("ask_memory", {"question": "x"})]):
            core.send("me", "A", "what do you know?")
    log = get_settings().llm_log_path.read_text(encoding="utf-8").splitlines()
    purposes = [json.loads(line)["purpose"] for line in log]
    assert purposes.count("memory_search") == 2 and "chat" in purposes
    assert all(json.loads(line)["thread_id"] == "A" for line in log)


# --- no injection, shared pipeline --------------------------------------------------------------


def test_nothing_about_the_user_is_injected_into_chat_requests():
    _remember_manager()
    requests: list = []

    def fn(messages, info: AgentInfo) -> ModelResponse:
        requests.append(list(messages))
        return ModelResponse(parts=[TextPart("ok")])

    with get_agent().override(model=build_model(get_settings(), raw=FunctionModel(fn))):
        core.send("me", "B", "Who's my manager?")
    [req] = requests
    last = req[-1]
    assert "Priya" not in (last.instructions or "")
    assert prompt_text(last.parts[-1].content) == "Who's my manager?"
    assert len(req) == 1  # session B has no history; nothing else was added


def test_user_message_survives_a_failed_run():
    def boom(messages, info):
        raise RuntimeError("provider down")

    with get_agent().override(model=build_model(get_settings(), raw=FunctionModel(boom))):
        with pytest.raises(RuntimeError):
            core.send("me", "A", "Remember this one thing.")
    [msg] = store.thread_messages("A")
    assert msg.text == "Remember this one thing." and msg.role == "user"
    assert records.search_memory(A, "remember thing", store="session")  # indexed too


def test_ingest_runs_note_maintenance_on_the_shared_path():
    settings = get_settings()
    total = settings.summary_chunk_turns + settings.summary_keep_recent_turns
    for i in range(total):
        core.ingest("me", "slack-dm", f"filler {i}", reply="ok")
    core.flush("me", "slack-dm")
    caller = MemoryCaller("me", "slack-dm")
    [note] = [h for h in records.search_memory(caller, "summary") if h.type == "note"]
    view = records.read(caller, note.ref)
    assert len(view.covers) == 2 * settings.summary_chunk_turns  # user + reply per turn


def test_stored_run_replays_tool_exchanges_without_instructions():
    with chat([("remember", remember_args())]):
        core.send("me", "A", "My manager is Priya Raman.")
    run = store.thread_messages("A")[-1].run_messages_json
    assert "remember" in run and '"instructions":null' in run.replace(" ", "")
    returns = [
        p
        for m in store.recent_history("A", budget=10_000, step=1, chars_per_token=3.0).messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, ToolReturnPart)
    ]
    assert returns and "Saved rec:" in str(returns[0].content)


def test_last_step_stops_tools_and_limit_returns_closest_evidence(monkeypatch):
    monkeypatch.setenv("MEMORY_SEARCH_MAX_REQUESTS", "4")
    get_settings.cache_clear()
    core.ingest("me", "A", "We finally moved! New place is on Birch Road.", reply="ok")
    seen: list[str] = []
    endless = [("search", {"query": "moved"})] * 10  # never answers on its own
    with search_agent_answering({"status": "found", "answer": "x"}, steps=endless, seen=seen):
        answer = core._run_sync(subagent.ask(A, "Where does the user live?"))
    assert seen[0].startswith("msg:")  # real results while steps remain
    assert seen[-1].startswith("Step budget used up")  # the last request gets no new results
    assert answer.status == "incomplete"
    assert answer.sources and "Birch Road" in answer.sources[0].excerpt  # closest evidence kept
