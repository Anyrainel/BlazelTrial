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
    model = build_model(settings, raw=TestModel(custom_output_text="hello there"))
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
    agent = Agent(build_model(settings, raw=reply_with_usage(9000)))

    with call_context(thread_id="slack-dm", purpose="test"), pytest.raises(BudgetExceededError):
        agent.run_sync("hi")

    [entry] = read_log(settings)
    assert entry["error"] == "reported_over_budget"
    assert entry["within_budget"] is False
    assert (entry["thread_id"], entry["purpose"]) == ("slack-dm", "test")


def test_reported_over_budget_only_logged_when_not_strict(settings):
    settings.budget_strict = False
    agent = Agent(build_model(settings, raw=reply_with_usage(9000)))

    assert agent.run_sync("hi").output == "ok"
    [entry] = read_log(settings)
    assert entry["within_budget"] is False
    assert entry["cache_read_tokens"] == 10


def test_limit_is_configurable_from_env(monkeypatch):
    monkeypatch.setenv("MAX_INPUT_TOKENS", "1234")
    from chatbot.config import Settings

    assert Settings(_env_file=None).max_input_tokens == 1234
