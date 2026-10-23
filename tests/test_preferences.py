"""Standing preferences: the one memory that is always injected, capped, and edited via memory."""

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from chatbot import core
from chatbot.agent.agent import get_agent, preference_block, preference_reserve_tokens
from chatbot.agent.models import build_model
from chatbot.capabilities.memory import subagent
from chatbot.capabilities.memory.subagent import Mode
from chatbot.config import get_settings
from chatbot.memory import forget, records
from chatbot.memory import messages as store
from chatbot.memory.records import MemoryAccessError, MemoryCaller
from tests.fakes import scripted, worker_answering

A = MemoryCaller("me", "A")
B = MemoryCaller("me", "B")


def pref(caller, text, *, store_="long_term", key="p", **kw):
    core.ingest("me", caller.session_id, f"said: {text}", reply="ok")
    source = f"msg:{store.thread_messages(caller.session_id)[-2].id}"
    return records.write(
        caller, store=store_, kind="preference", key=key, text=text, sources=[source], **kw
    )


def test_preferences_are_injected_into_chat_requests_and_facts_are_not():
    pref(A, "Use American spelling.", key="spelling")
    pref(A, "Keep replies terse here.", store_="session", key="tone")
    records.write(
        A, store="long_term", kind="fact", key="manager", text="Manager is Priya Raman.",
        sources=[f"msg:{store.thread_messages('A')[0].id}"],
    )  # fmt: skip
    seen: list[str] = []

    def fn(messages, info: AgentInfo) -> ModelResponse:
        seen.append(messages[-1].instructions or "")
        return ModelResponse(parts=[TextPart("ok")])

    with get_agent().override(model=build_model(get_settings(), raw=FunctionModel(fn))):
        core.send("me", "A", "hello")
        core.send("me", "B", "hello")
    in_a, in_b = seen
    assert (
        "Use American spelling." in in_a
        and "Keep replies terse here. (this conversation only)" in in_a
    )
    assert "Use American spelling." in in_b and "terse" not in in_b  # session rule stays in A
    assert "Priya" not in in_a  # facts are still reached only through memory


def test_no_block_when_there_are_no_preferences():
    assert preference_block("me", "A") == ""


def test_a_preference_over_the_cap_is_refused_with_a_useful_error(monkeypatch):
    monkeypatch.setenv("PREFERENCES_MAX_CHARS", "60")
    get_settings.cache_clear()
    pref(A, "Use American spelling.", key="spelling")  # 22 chars
    with pytest.raises(MemoryAccessError, match="preference limit: 22 of 60"):
        pref(
            A,
            "Always answer in formal English with long, complete and polite sentences.",
            key="tone",
        )
    ok = pref(A, "Be brief.", key="tone")  # fits
    assert ok.text == "Be brief."


def test_revising_counts_only_the_new_size_and_retiring_frees_room(monkeypatch):
    monkeypatch.setenv("PREFERENCES_MAX_CHARS", "40")
    get_settings.cache_clear()
    first = pref(A, "Use British spelling please.", key="spelling")  # 28 chars
    revised = pref(A, "Use American spelling pls.", key="spelling", supersedes=first.ref)
    with pytest.raises(MemoryAccessError, match="preference limit"):
        pref(A, "Short but long enough.", key="tone")
    records.retire(A, revised.ref)
    assert pref(A, "Short but long enough.", key="tone").text.startswith("Short")
    assert [v.text for v in records.current_preferences(A)] == ["Short but long enough."]


def test_retired_records_keep_their_history_and_cannot_be_revised():
    first = pref(A, "Use British spelling.", key="spelling")
    records.retire(A, first.ref)
    assert not records.read(A, first.ref).current
    assert [v.text for v in records.versions(A, first.ref)] == ["Use British spelling."]
    with pytest.raises(MemoryAccessError):
        records.retire(A, first.ref)  # already retired
    with pytest.raises(MemoryAccessError, match="not the current version"):
        pref(A, "Use American spelling.", key="spelling", supersedes=first.ref)


def test_session_preferences_have_their_own_smaller_cap(monkeypatch):
    monkeypatch.setenv("SESSION_PREFERENCES_MAX_CHARS", "20")
    get_settings.cache_clear()
    with pytest.raises(MemoryAccessError, match="session preferences"):
        pref(A, "This is far too long for one chat.", store_="session", key="tone")
    pref(A, "Be playful.", store_="session", key="tone")


def test_the_worker_can_retire_and_is_told_about_the_limit():
    first = pref(A, "Use British spelling.", key="spelling")
    seen: list[str] = []
    steps = [("retire", {"record": first.ref}), ("retire", {"record": first.ref})]
    final = {"status": "retired", "summary": "Dropped it."}
    with worker_answering(Mode.WRITE, final, steps, seen):
        outcome, deps = core._run_sync(
            subagent.work(Mode.WRITE, A, "Memory update: drop British spelling")
        )
    assert seen[0].startswith("Retired rec:") and seen[1].startswith("Not retired:")
    assert outcome.status == "retired" and deps.actions == [
        f"retired {first.ref} (long_term preference 'spelling'): Use British spelling."
    ]


def test_forgetting_a_value_removes_the_preference_that_contains_it():
    pref(A, "Sign messages as Captain Velvet Otter.", key="signature")
    forget.forget_values("me", ["Captain Velvet Otter"])
    assert preference_block("me", "A") == ""


def test_the_context_budget_reserves_room_for_the_worst_case_block():
    settings = get_settings()
    chars = settings.preferences_max_chars + settings.session_preferences_max_chars
    assert preference_reserve_tokens() >= chars / settings.estimate_chars_per_token
    assert preference_reserve_tokens() < 700  # small next to the 8k budget


def test_chat_can_still_update_a_preference_through_the_tool():
    final = {"status": "saved", "summary": "ok"}
    args = {
        "store": "long_term",
        "kind": "preference",
        "key": "spelling",
        "text": "Use American spelling.",
    }
    chat = build_model(
        get_settings(),
        raw=scripted([("update_memory", {"change": "Use American spelling."})], "Done."),
    )
    with worker_answering(Mode.WRITE, final, [("save", args)]), get_agent().override(model=chat):
        core.send("me", "A", "Always use American spelling.")
    assert preference_block("me", "B").endswith("- Use American spelling.")


def test_the_chat_request_has_exactly_one_system_message_even_with_preferences():
    """Chat templates such as Bonsai's reject a second system message (found live: HTTP 500)."""
    import asyncio

    from pydantic_ai.models import ModelRequestParameters
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    pref(A, "Use American spelling.", key="spelling")
    captured: list = []

    def fn(messages, info: AgentInfo) -> ModelResponse:
        captured.extend(messages)
        return ModelResponse(parts=[TextPart("ok")])

    with get_agent().override(model=build_model(get_settings(), raw=FunctionModel(fn))):
        core.send("me", "A", "hello")
    local = OpenAIChatModel("m", provider=OpenAIProvider(base_url="http://x/v1", api_key="k"))
    chat = asyncio.run(local._map_messages(captured, ModelRequestParameters(), model_settings={}))
    roles = [m["role"] for m in chat]
    assert roles.count("system") == 1 and roles[0] == "system"
    assert "Use American spelling." in chat[0]["content"]
