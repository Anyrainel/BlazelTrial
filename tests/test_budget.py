import json

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RequestUsage

from chatbot import core
from chatbot.agent.agent import get_agent
from chatbot.agent.budget import BudgetExceededError, call_context
from chatbot.agent.models import build_model


def read_log(settings) -> list[dict]:
    lines = settings.llm_log_path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def reply_with_usage(input_tokens: int, calls: list | None = None) -> FunctionModel:
    def fn(messages, info: AgentInfo) -> ModelResponse:
        if calls is not None:
            calls.append(messages)
        return ModelResponse(
            parts=[TextPart("ok")],
            usage=RequestUsage(input_tokens=input_tokens, cache_read_tokens=10, output_tokens=2),
        )

    return FunctionModel(fn)


def test_send_goes_through_budget_and_logs(settings):
    model = build_model(settings, raw=TestModel(custom_output_text="hello there", call_tools=[]))
    with get_agent().override(model=model):
        reply = core.send("me", "s1", "hi")

    assert reply == "hello there"
    [entry] = [e for e in read_log(settings) if e["purpose"] == "chat"]
    assert entry["thread_id"] == "s1"
    assert entry["purpose"] == "chat"
    assert entry["limit"] == 8000
    assert entry["preflight_method"] == "estimate"  # TestModel has no count endpoint
    assert entry["input_tokens"] > 0
    assert entry["within_budget"] is True


def test_preflight_refuses_before_sending(settings):
    settings.max_input_tokens = 50
    calls: list = []
    agent = Agent(build_model(settings, raw=reply_with_usage(10, calls)))

    with pytest.raises(BudgetExceededError) as exc:
        agent.run_sync("x" * 2000)

    assert exc.value.stage == "preflight"
    assert calls == []  # the provider was never called
    [entry] = read_log(settings)
    assert entry["error"] == "preflight_over_budget"
    assert entry["input_tokens"] is None


def test_reported_usage_over_budget_raises_in_strict_mode(settings):
    settings.budget_strict = True
    agent = Agent(build_model(settings, raw=reply_with_usage(9000)))

    with call_context(thread_id="slack-dm", purpose="test"), pytest.raises(BudgetExceededError):
        agent.run_sync("hi")

    [entry] = read_log(settings)
    assert entry["error"] == "reported_over_budget"
    assert entry["within_budget"] is False
    assert (entry["thread_id"], entry["purpose"]) == ("slack-dm", "test")


def test_reported_over_budget_only_logged_when_not_strict(settings):
    agent = Agent(build_model(settings, raw=reply_with_usage(9000)))

    assert agent.run_sync("hi").output == "ok"
    [entry] = read_log(settings)
    assert entry["within_budget"] is False
    assert entry["cache_read_tokens"] == 10


def test_limit_is_configurable_from_env(monkeypatch):
    monkeypatch.setenv("MAX_INPUT_TOKENS", "1234")
    from chatbot.config import Settings

    assert Settings(_env_file=None).max_input_tokens == 1234


def test_estimate_safety_factor_scales_preflight(settings):
    settings.max_input_tokens = 100
    settings.estimate_safety_factor = 1.0
    text = "x" * 240  # ~80 raw estimated tokens of content plus overhead
    assert Agent(build_model(settings, raw=reply_with_usage(10))).run_sync(text).output == "ok"

    settings.estimate_safety_factor = 2.0
    with pytest.raises(BudgetExceededError):
        Agent(build_model(settings, raw=reply_with_usage(10))).run_sync(text)


def test_sync_wrappers_share_one_open_event_loop():
    import asyncio

    async def current_loop():
        return asyncio.get_running_loop()

    first = core._run_sync(current_loop())
    second = core._run_sync(current_loop())
    assert first is second and not first.is_closed()


def test_estimate_counts_only_the_latest_instructions():
    from pydantic_ai.messages import ModelRequest, UserPromptPart
    from pydantic_ai.models import ModelRequestParameters

    from chatbot.agent.budget import estimate_tokens

    big = "x" * 3000
    one = [ModelRequest(parts=[UserPromptPart("hi")], instructions=big)]
    many = [ModelRequest(parts=[UserPromptPart("hi")], instructions=big) for _ in range(5)]
    params = ModelRequestParameters()
    single = estimate_tokens(one, params, 3.0)
    assert estimate_tokens(many, params, 3.0) < single + 5 * 20  # not 5x the instructions


# --- resilience: shrink instead of failing, exact counters, learned scale ----------------------


def _bounded(settings, limit, calls=None, **kwargs):
    settings.max_input_tokens = limit
    return build_model(settings, raw=reply_with_usage(10, calls)), settings


def test_oversized_message_is_shrunk_and_sent_not_refused(settings):
    calls: list = []
    model, _ = _bounded(settings, 500, calls)
    huge = "start " + "word " * 3000 + " the-end"

    assert Agent(model).run_sync(huge).output == "ok"

    sent = calls[0][-1].parts[-1].content
    assert len(sent) < len(huge) and "omitted to fit the context window" in sent
    assert sent.startswith("start") and sent.endswith("the-end")  # head and tail survive
    [entry] = read_log(settings)
    assert entry["error"] is None and entry["shrunk_from"] > 500
    assert entry["preflight_tokens"] < 500


def test_big_tool_results_and_old_turns_are_trimmed_without_splitting_calls():
    from pydantic_ai.messages import (
        ModelRequest,
        ToolCallPart,
        ToolReturnPart,
        UserPromptPart,
    )

    from chatbot.agent.shrink import shrink_messages

    def turn(n: int, payload: str):
        return [
            ModelRequest(parts=[UserPromptPart(f"question {n}")]),
            ModelResponse(parts=[ToolCallPart("lookup", {"q": n}, tool_call_id=f"c{n}")]),
            ModelRequest(parts=[ToolReturnPart("lookup", payload, tool_call_id=f"c{n}")]),
            ModelResponse(parts=[TextPart(f"answer {n}")]),
        ]

    history = [m for n in range(1, 6) for m in turn(n, "x" * 6000)]
    history.append(ModelRequest(parts=[UserPromptPart("latest question")]))

    def size(ms):
        return sum(len(str(getattr(p, "content", ""))) + 40 for m in ms for p in m.parts) // 3

    shrunk = shrink_messages(history, size, 600)

    assert size(shrunk) <= 600
    assert shrunk[-1].parts[0].content == "latest question"  # the current message stays whole
    calls = {p.tool_call_id for m in shrunk for p in m.parts if isinstance(p, ToolCallPart)}
    returns = {p.tool_call_id for m in shrunk for p in m.parts if isinstance(p, ToolReturnPart)}
    assert calls == returns  # a call never loses its return
    assert len(history) == 21  # the stored history itself is untouched


def test_request_that_cannot_shrink_is_still_refused_before_sending(settings):
    calls: list = []
    model, _ = _bounded(settings, 100, calls)
    agent = Agent(model, instructions="fixed instructions " * 400)

    with pytest.raises(BudgetExceededError) as exc:
        agent.run_sync("hi")

    assert exc.value.stage == "preflight" and calls == []


def test_the_limit_is_exclusive(settings):
    import asyncio

    from pydantic_ai.messages import ModelRequest, UserPromptPart
    from pydantic_ai.models import ModelRequestParameters

    from chatbot.agent.budget import BudgetedModel, CallLog

    def preflight(counted: int):
        model = BudgetedModel(
            reply_with_usage(1),
            max_input_tokens=100,
            use_server_count=False,
            chars_per_token=3.0,
            strict=False,
            log=CallLog(settings.llm_log_path),
            counter=_fixed_counter(counted),
            counter_margin=1.0,
        )
        messages = [ModelRequest(parts=[UserPromptPart("hi")])]
        return asyncio.run(model.preflight(messages, None, ModelRequestParameters()))

    assert preflight(98) == (99, "tokenizer")  # 99 tokens: below 100
    with pytest.raises(BudgetExceededError):
        preflight(99)  # 100 tokens would not be below the limit


def _fixed_counter(n: int):
    async def count(messages, params):
        return n

    return count


def test_tokenizer_count_is_used_and_falls_back_to_the_estimate(settings):
    import asyncio

    from pydantic_ai.messages import ModelRequest, UserPromptPart
    from pydantic_ai.models import ModelRequestParameters

    from chatbot.agent.budget import BudgetedModel, CallLog

    async def broken(messages, params):
        raise OSError("server down")

    def make(counter):
        return BudgetedModel(
            reply_with_usage(1),
            max_input_tokens=8000,
            use_server_count=False,
            chars_per_token=3.0,
            strict=False,
            log=CallLog(settings.llm_log_path),
            counter=counter,
        )

    messages = [ModelRequest(parts=[UserPromptPart("hi")])]
    params = ModelRequestParameters()
    assert asyncio.run(make(_fixed_counter(500)).measure(messages, None, params)).method == (
        "tokenizer"
    )
    assert asyncio.run(make(broken).measure(messages, None, params)).method == "estimate"


def test_a_low_count_raises_that_methods_scale(settings):
    model = build_model(settings, raw=reply_with_usage(900))  # estimate said ~12, reality 900
    before = model.scale["estimate"]

    Agent(model).run_sync("hi")

    assert model.scale["estimate"] > before


def test_an_exact_or_generous_count_leaves_the_scale_alone(settings):
    import asyncio

    from pydantic_ai.messages import ModelRequest, UserPromptPart
    from pydantic_ai.models import ModelRequestParameters

    from chatbot.agent.budget import BudgetedModel, CallLog

    model = BudgetedModel(
        reply_with_usage(300),
        max_input_tokens=8000,
        use_server_count=False,
        chars_per_token=3.0,
        strict=False,
        log=CallLog(settings.llm_log_path),
        counter=_fixed_counter(300),  # exact: the provider then reports 300
        counter_margin=1.05,
    )
    messages = [ModelRequest(parts=[UserPromptPart("hi")])]
    asyncio.run(model.request(messages, None, ModelRequestParameters()))
    assert model.scale["tokenizer"] == 1.05
