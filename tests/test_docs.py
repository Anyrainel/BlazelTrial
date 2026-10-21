import asyncio

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
from chatbot.agent.models import build_model
from chatbot.config import get_settings
from chatbot.docs_reader import reader
from scenarios.fixtures.docs import Q3_LAUNCH_DATE, write_all
from tests.fakes import prompt_text


@pytest.fixture
def docs():
    write_all(get_settings().docs_path)


class NoteModel:
    """Map/reduce stand-in: map notes keep lines with a year; reduce joins them."""

    def __init__(self):
        self.calls = 0

    def model(self):
        def fn(messages, info: AgentInfo) -> ModelResponse:
            self.calls += 1
            text = prompt_text(messages[-1].parts[-1].content)
            keep = [line for line in text.splitlines() if "2026" in line]
            return ModelResponse(parts=[TextPart("\n".join(keep) or "no concrete content")])

        return build_model(get_settings(), raw=FunctionModel(fn))


def tool_then_answer(tool: str, args: dict, seen: list[str]) -> FunctionModel:
    """Chat model that calls one tool, then echoes the tool output as its answer."""

    def fn(messages, info: AgentInfo) -> ModelResponse:
        returns = [
            p
            for m in messages
            if isinstance(m, ModelRequest)
            for p in m.parts
            if isinstance(p, ToolReturnPart)
        ]
        if not returns:
            return ModelResponse(parts=[ToolCallPart(tool, args)])
        seen.append(str(returns[-1].content))
        return ModelResponse(parts=[TextPart("done")])

    return FunctionModel(fn)


def test_resolve_is_forgiving_but_never_opens_arbitrary_paths(docs):
    assert reader.resolve("q3 plan").name == "Q3-plan.md"
    assert reader.resolve("Q3-plan.md").name == "Q3-plan.md"
    for bad in ["../secrets", "/etc/passwd", "..\\x", "nope"]:
        with pytest.raises(reader.DocNotFoundError):
            reader.resolve(bad)


def test_docs_are_big_and_passages_are_bounded(docs):
    doc = reader.load("Q3-plan")
    assert doc.approx_tokens > 20_000
    parts = reader.passages(doc.text, 1200)
    assert all(len(p) <= 1200 for _, p in parts)
    assert any(Q3_LAUNCH_DATE in p for _, p in parts)
    max_chars = 2500 * 3
    assert all(len(c) <= max_chars + 200 for c in reader.chunks(doc.text, max_chars))


def test_fetch_doc_returns_a_cached_note_with_the_key_dates(docs):
    notes = NoteModel()
    seen: list[str] = []
    chat = build_model(
        get_settings(), raw=tool_then_answer("fetch_doc", {"doc_id": "q3-plan"}, seen)
    )
    with (
        reader.get_mapper().override(model=notes.model()),
        reader.get_reducer().override(model=notes.model()),
        get_agent().override(model=chat),
    ):
        core.send("me", "slack-dm", "Summarize doc Q3-plan.")
        first_calls = notes.calls
        core.send("me", "slack-dm", "Summarize it again.")

    assert first_calls > 5  # map over many chunks + reduce
    assert notes.calls == first_calls  # second fetch served from the cached note
    assert Q3_LAUNCH_DATE in seen[0]
    budget_chars = 2 * 1500 * get_settings().estimate_chars_per_token
    assert len(seen[0]) < budget_chars  # tool output is bounded, never the whole doc


def test_search_doc_finds_the_launch_passage(docs):
    seen: list[str] = []
    args = {"doc_id": "Q3-plan", "query": "public launch date"}
    chat = build_model(get_settings(), raw=tool_then_answer("search_doc", args, seen))
    with get_agent().override(model=chat):
        core.send("me", "A", "When is the launch?")
    assert Q3_LAUNCH_DATE in seen[0]
    assert len(seen[0]) <= get_settings().doc_search_token_budget * 3 + 200


def test_opened_docs_are_listed_in_later_turns(docs):
    seen: list[str] = []
    chat = build_model(
        get_settings(),
        raw=tool_then_answer("search_doc", {"doc_id": "Q3-plan", "query": "budget"}, seen),
    )
    with get_agent().override(model=chat):
        core.send("me", "slack-dm", "What's the budget in the Q3 plan?")

    instructions: list[str] = []

    def fn(messages, info):
        instructions.append(messages[-1].instructions or "")
        return ModelResponse(parts=[TextPart("ok")])

    with get_agent().override(model=build_model(get_settings(), raw=FunctionModel(fn))):
        core.send("me", "slack-dm", "And the launch date in that plan?")
    assert "Documents opened in this conversation" in instructions[0]
    assert "- Q3-plan" in instructions[0]


def test_unknown_doc_lists_what_exists(docs):
    from chatbot.capabilities.docs import _unknown

    msg = _unknown("roadmap")
    assert "Q3-plan" in msg and "brand-guidelines" in msg


def test_small_docs_come_back_whole(docs):
    path = get_settings().docs_path / "tiny.md"
    path.write_text("# Tiny\n\nThe offsite is on May 3.", encoding="utf-8")
    from chatbot.agent.agent import ChatDeps
    from chatbot.capabilities import docs as tools

    class Ctx:
        deps = ChatDeps("me", "A")

    out = asyncio.run(tools.fetch_doc(Ctx(), "tiny"))
    assert "full text" in out and "May 3" in out


def test_reduce_converges_even_when_outputs_are_large():
    from chatbot.docs_reader.reader import Doc, _reduce, get_reducer

    calls = 0

    def fn(messages, info):
        nonlocal calls
        calls += 1
        return ModelResponse(parts=[TextPart("z" * 5000)])  # every output bigger than half

    notes = ["n" * 4000 for _ in range(16)]
    with get_reducer().override(model=build_model(get_settings(), raw=FunctionModel(fn))):
        asyncio.run(_reduce(Doc("d", "", "x"), notes, max_chars=7500))
    assert calls <= 16  # 8 + 4 + 2 + 1 = 15 at most, never a runaway loop


def test_parallel_fetches_compute_the_note_once(docs):
    notes = NoteModel()
    doc = reader.load("Q3-plan")

    async def both():
        return await asyncio.gather(reader.get_note(doc), reader.get_note(doc))

    with (
        reader.get_mapper().override(model=notes.model()),
        reader.get_reducer().override(model=notes.model()),
    ):
        a, b = asyncio.run(both())
        single = notes.calls
    assert a == b
    parts = len(reader.chunks(doc.text, int(2500 * get_settings().estimate_chars_per_token)))
    assert single < 2 * parts  # one map pass (+ a few reduces), not two
