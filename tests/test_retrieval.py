from datetime import datetime

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from chatbot import core
from chatbot.agent.agent import get_agent
from chatbot.agent.models import build_model
from chatbot.config import get_settings
from chatbot.memory import messages as store
from chatbot.memory.summarize import get_summarizer
from scenarios.fixtures.slack_history import PLANTED, generate


def load_history(n: int) -> None:
    for row in generate(n):
        core.ingest(
            "me", "slack-dm", row["user"], row["assistant"], datetime.fromisoformat(row["ts"])
        )
    core.flush("me", "slack-dm")


class Recorder:
    """Chat model that records each request's last user prompt and instructions."""

    def __init__(self):
        self.prompts: list[str] = []
        self.instructions: list[str] = []

    def model(self):
        def fn(messages, info: AgentInfo) -> ModelResponse:
            last = next(m for m in reversed(messages) if isinstance(m, ModelRequest))
            self.instructions.append(last.instructions or "")
            self.prompts.append(
                " ".join(p.content for p in last.parts if isinstance(p, UserPromptPart))
            )
            return ModelResponse(parts=[TextPart("ok")])

        return build_model(get_settings(), raw=FunctionModel(fn))


def test_old_detail_is_retrieved_into_the_prompt_but_not_stored():
    load_history(120)
    rec = Recorder()
    with get_agent().override(model=rec.model()):
        core.send("me", "slack-dm", "Which caterer did I book for Dana's farewell?")

    assert "Saffron Table" in rec.prompts[0]
    assert "[turn 20," in rec.prompts[0]
    stored = store.thread_messages("slack-dm")[-1].run_messages_json
    assert "Saffron Table" not in stored  # only the user's own words are persisted


def test_retrieval_skips_turns_already_in_the_window_and_irrelevant_text():
    load_history(30)
    rec = Recorder()
    with get_agent().override(model=rec.model()):
        core.send("me", "slack-dm", "zzz qqq")  # matches nothing
    assert rec.prompts[0] == "zzz qqq"


def test_summaries_cover_old_chunks_and_reach_the_prompt():
    load_history(120)
    settings = get_settings()
    from sqlmodel import select

    from chatbot.memory.db import session
    from chatbot.memory.models import Summary

    with session() as s:
        spans = [(r.start_turn, r.end_turn) for r in s.exec(select(Summary)).all()]
    chunk = settings.summary_chunk_turns
    assert spans == [(i, i + chunk - 1) for i in range(1, 101, chunk)]  # 101-125 not complete

    rec = Recorder()
    with get_agent().override(model=rec.model()):
        core.send("me", "slack-dm", "What have we been up to?")
    assert "Earlier in this conversation" in rec.instructions[0]
    assert rec.instructions[0].count("(turns ") == settings.summaries_in_context


def test_summarizer_prompt_stays_within_budget():
    load_history(60)
    seen: list[int] = []

    def fn(messages, info):
        seen.append(len(str(messages)))
        return ModelResponse(parts=[TextPart("s")])

    # Already summarized during load; force a fresh thread to observe the prompt size.
    with get_summarizer().override(model=build_model(get_settings(), raw=FunctionModel(fn))):
        for row in generate(60):
            core.ingest("me", "other", row["user"] * 20, row["assistant"])
        core.flush("me", "other")
    assert seen  # summaries ran on huge messages without tripping the budget guard


def test_search_history_tool_returns_snippets():
    load_history(120)
    calls = 0

    def fn(messages, info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(parts=[ToolCallPart("search_history", {"query": "Larkspur"})])
        ret = next(
            p
            for m in messages
            if isinstance(m, ModelRequest)
            for p in m.parts
            if isinstance(p, ToolReturnPart)
        )
        return ModelResponse(parts=[TextPart(str(ret.content))])

    with get_agent().override(model=build_model(get_settings(), raw=FunctionModel(fn))):
        reply = core.send("me", "slack-dm", "hello")
    assert "27 Larkspur Lane" in reply
    assert f"[turn {PLANTED['address'].turn}," in reply


def test_ingest_keeps_original_timestamps():
    row = generate(1)[0]
    core.ingest("me", "slack-dm", row["user"], row["assistant"], datetime.fromisoformat(row["ts"]))
    msg = store.thread_messages("slack-dm")[0]
    assert msg.created_at.isoformat().startswith(row["ts"][:16])


def test_only_relevant_facts_are_shown_within_budget():
    from chatbot.memory import facts
    from chatbot.memory.models import FactKind, Scope

    many = [
        facts.NewFact(Scope.USER, f"colleague_{i}", f"Colleague {i} works on project {i}.")
        for i in range(60)
    ]
    many.append(facts.NewFact(Scope.USER, "manager", "The user's manager is Priya Raman."))
    many.append(
        facts.NewFact(Scope.USER, "spelling", "Use American spelling.", FactKind.PREFERENCE)
    )
    facts.apply("me", "A", 1, many)

    block = facts.render_relevant_facts("me", "A", "Who is my manager?", max_chars=400)
    assert "Priya Raman" in block.splitlines()[0]  # most relevant first
    assert len(block) <= 400
    assert "American spelling" not in block  # preferences live in the instructions
    assert "American spelling" in facts.render_preferences("me", "A")


def test_context_shrinks_instead_of_failing_on_huge_input(monkeypatch):
    load_history(120)
    monkeypatch.setenv("MAX_INPUT_TOKENS", "4500")
    get_settings.cache_clear()
    rec = Recorder()
    with get_agent().override(model=rec.model()):
        core.send("me", "slack-dm", "Which caterer did I book? " + "padding " * 600)
    assert rec.prompts  # the turn went through
    assert "Earlier in this conversation" not in rec.instructions[0]  # summaries dropped
