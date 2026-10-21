import json

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from chatbot import core
from chatbot.agent.agent import get_agent
from chatbot.agent.models import build_model
from chatbot.config import get_settings
from chatbot.memory import messages as store
from chatbot.memory.messages import choose_window_start
from chatbot.memory.models import Role, Source
from tests.fakes import prompt_text


def recording_model(calls: list[list]) -> FunctionModel:
    """Replies 'reply N' and records the messages each request saw."""

    def fn(messages, info: AgentInfo) -> ModelResponse:
        calls.append(messages)
        return ModelResponse(parts=[TextPart(f"reply {len(calls)}")])

    return FunctionModel(fn)


def user_texts(messages) -> list[str]:
    return [
        prompt_text(p.content)
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, UserPromptPart)
    ]


def agent_with(calls):
    return get_agent().override(model=build_model(get_settings(), raw=recording_model(calls)))


def test_send_persists_both_sides_of_the_turn():
    calls: list = []
    with agent_with(calls):
        core.send("me", "s1", "hello")

    rows = store.thread_messages("s1")
    assert [(r.turn, r.role, r.text) for r in rows] == [
        (1, Role.USER, "hello"),
        (1, Role.ASSISTANT, "reply 1"),
    ]
    assert rows[1].source == Source.LIVE
    assert json.loads(rows[1].run_messages_json)  # full PydanticAI run stored for replay


def test_history_is_replayed_within_the_same_thread_only():
    calls: list = []
    with agent_with(calls):
        core.send("me", "A", "my cat is Mochi")
        core.send("me", "A", "what is my cat called?")
        core.send("me", "B", "what is my cat called?")

    assert user_texts(calls[1]) == ["my cat is Mochi", "what is my cat called?"]
    assert user_texts(calls[2]) == ["what is my cat called?"]  # session B starts fresh


def test_ingest_uses_same_path_without_calling_the_model():
    calls: list = []
    with agent_with(calls):
        core.ingest("me", "slack-dm", "I live at 1 Elm St", reply="Noted.")
        core.ingest("me", "slack-dm", "just a thought")  # no reply recorded
        core.send("me", "slack-dm", "where do I live?")

    assert len(calls) == 1
    assert user_texts(calls[0]) == ["I live at 1 Elm St", "just a thought", "where do I live?"]
    rows = store.thread_messages("slack-dm")
    assert [(r.turn, r.source) for r in rows[:3]] == [
        (1, Source.INGEST),
        (1, Source.INGEST),
        (2, Source.INGEST),
    ]


def test_history_window_is_bounded_for_long_threads(monkeypatch):
    monkeypatch.setenv("HISTORY_TOKEN_BUDGET", "400")
    get_settings.cache_clear()
    for i in range(1, 101):
        core.ingest("me", "slack-dm", f"message number {i} " + "x" * 100, reply="ok")

    calls: list = []
    with agent_with(calls):
        core.send("me", "slack-dm", "latest")

    seen = user_texts(calls[0])
    assert seen[-1] == "latest"
    assert 1 < len(seen) < 20  # bounded, far fewer than 100 turns
    first = int(seen[0].split()[2])
    assert (first - 1) % get_settings().history_window_step == 0  # window start is aligned


def test_window_start_snaps_to_step_and_stays_stable():
    sizes = [(t, 10) for t in range(1, 21)]  # 20 turns, 10 tokens each
    # budget fits the last 7 turns (14..20) -> snaps forward to 17 (k*8+1)
    assert choose_window_start(sizes, budget=70, step=8) == 17
    # one more turn: earliest fitting is 15, still snaps to 17 -> same prefix
    assert choose_window_start([*sizes, (21, 10)], budget=70, step=8) == 17
    # nothing fits
    assert choose_window_start(sizes, budget=5, step=8) is None
    # only the latest turn fits and the aligned start is past it -> fall back to earliest fit
    assert choose_window_start(sizes, budget=10, step=8) == 20


def test_cache_point_sits_before_the_current_message_only_with_history():
    from pydantic_ai.messages import CachePoint

    seen: list = []

    def fn(messages, info: AgentInfo) -> ModelResponse:
        last = messages[-1]
        seen.append(next(p.content for p in last.parts if isinstance(p, UserPromptPart)))
        return ModelResponse(parts=[TextPart("ok")])

    with get_agent().override(model=build_model(get_settings(), raw=FunctionModel(fn))):
        core.send("me", "A", "first")  # no history: nothing to attach a breakpoint to
        core.send("me", "A", "second")

    assert seen[0] == "first"
    assert isinstance(seen[1][0], CachePoint) and seen[1][1] == "second"
    stored = store.thread_messages("A")[-1].run_messages_json
    assert "cache-point" not in stored  # markers are never persisted


def test_stored_runs_drop_instructions_and_replay_shrinks_tool_output():
    from pydantic_ai.messages import ModelMessagesTypeAdapter, ToolCallPart, ToolReturnPart

    calls = 0

    def fn(messages, info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(parts=[ToolCallPart("search_history", {"query": "anything"})])
        return ModelResponse(parts=[TextPart("ok")])

    long_text = "Larkspur " * 300
    core.ingest("me", "A", long_text, reply="noted")
    with get_agent().override(model=build_model(get_settings(), raw=FunctionModel(fn))):
        core.send("me", "A", "look it up")

    stored = ModelMessagesTypeAdapter.validate_json(
        store.thread_messages("A")[-1].run_messages_json
    )
    assert all(getattr(m, "instructions", None) is None for m in stored)

    window = store.recent_history("A", budget=100_000, step=1, chars_per_token=3.0)
    returns = [p for m in window.messages for p in m.parts if isinstance(p, ToolReturnPart)]
    assert returns and all(len(p.model_response_str()) < 600 for p in returns)
