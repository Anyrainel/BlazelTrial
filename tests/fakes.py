"""Offline stand-ins for model calls."""

from contextlib import contextmanager

from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from chatbot.agent.models import build_model
from chatbot.capabilities.memory.subagent import Mode, get_worker
from chatbot.config import get_settings


def scripted(steps: list, final, seen: list | None = None) -> FunctionModel:
    """A model that makes the tool calls in `steps` (one (name, args) per request), then
    returns `final`: a dict (output-tool args) or a str (text). Tool results go to `seen`."""
    count = 0

    def fn(messages, info: AgentInfo) -> ModelResponse:
        nonlocal count
        if seen is not None and count:
            last = messages[-1]
            if isinstance(last, ModelRequest):
                seen.extend(str(p.content) for p in last.parts if isinstance(p, ToolReturnPart))
        count += 1
        if count <= len(steps):
            name, args = steps[count - 1]
            return ModelResponse(parts=[ToolCallPart(name, args)])
        if isinstance(final, dict):
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, final)])
        from pydantic_ai.messages import TextPart

        return ModelResponse(parts=[TextPart(final)])

    return FunctionModel(fn)


@contextmanager
def worker_answering(mode: Mode, final: dict, steps: list | None = None, seen: list | None = None):
    """Script the memory worker of one mode: tool calls in `steps`, then the output `final`."""
    model = build_model(get_settings(), raw=scripted(steps or [], final, seen))
    with get_worker(mode).override(model=model):
        yield


def search_agent_answering(final: dict, steps: list | None = None, seen: list | None = None):
    return worker_answering(Mode.READ, final, steps, seen)


def prompt_text(content) -> str:
    """Text of a user prompt, ignoring markers such as CachePoint."""
    if isinstance(content, str):
        return content
    return " ".join(c for c in content if isinstance(c, str))
