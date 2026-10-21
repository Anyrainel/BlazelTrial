"""Offline stand-ins for model calls."""

from contextlib import contextmanager

from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from chatbot.agent.models import build_model
from chatbot.config import get_settings
from chatbot.memory.extract import get_extractor


def extraction_model(script: list[list[dict]], seen: list | None = None) -> FunctionModel:
    """FunctionModel for the extractor: call N returns `script[N]` (the last entry repeats)."""
    count = 0

    def fn(messages, info: AgentInfo) -> ModelResponse:
        nonlocal count
        if seen is not None:
            seen.append(messages)
        items = script[min(count, len(script) - 1)] if script else []
        count += 1
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"items": items})])

    return FunctionModel(fn)


@contextmanager
def extractor_returning(script, seen: list | None = None):
    model = build_model(get_settings(), raw=extraction_model(script, seen))
    with get_extractor().override(model=model):
        yield


def prompt_text(content) -> str:
    """Text of a user prompt, ignoring markers such as CachePoint."""
    if isinstance(content, str):
        return content
    return " ".join(c for c in content if isinstance(c, str))
