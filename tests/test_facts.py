from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from chatbot import core
from chatbot.agent.agent import get_agent
from chatbot.agent.models import build_model
from chatbot.config import get_settings
from chatbot.memory import facts
from chatbot.memory import messages as store
from chatbot.memory.models import Scope
from tests.fakes import extractor_returning

MANAGER = {"scope": "user", "key": "manager", "text": "The user's manager is Priya Raman."}
BRITISH = {"scope": "user", "key": "spelling_preference", "text": "Use British spelling."}
AMERICAN = {"scope": "user", "key": "Spelling Preference", "text": "Use American spelling."}
PLAYFUL = {"scope": "ephemeral", "key": "draft_tone", "text": "Make this draft playful."}
TERSE = {"scope": "thread", "key": "reply_style", "text": "Keep replies terse in this chat."}


def chat_model(instructions_seen: list[str]) -> FunctionModel:
    def fn(messages, info: AgentInfo) -> ModelResponse:
        last = next(m for m in reversed(messages) if isinstance(m, ModelRequest))
        instructions_seen.append(last.instructions or "")
        return ModelResponse(parts=[TextPart("ok")])

    return FunctionModel(fn)


def chat_with(seen: list[str]):
    return get_agent().override(model=build_model(get_settings(), raw=chat_model(seen)))


def test_user_fact_reaches_other_sessions_and_slack():
    seen: list[str] = []
    with chat_with(seen):
        core.send("me", "B", "hi")  # session B opened before the fact is told
        with extractor_returning([[MANAGER], []]):
            core.send("me", "A", "My manager is Priya Raman.")
        core.send("me", "B", "Who's my manager?")
        core.send("me", "slack-dm", "Who's my manager?")

    assert "Priya Raman" not in seen[0]
    assert "Priya Raman" in seen[2]
    assert "Priya Raman" in seen[3]


def test_changed_preference_supersedes_and_keeps_history():
    with extractor_returning([[BRITISH]]):
        core.send("me", "slack-dm", "Always use British spelling.")
    with extractor_returning([[AMERICAN]]):
        core.send("me", "slack-dm", "Switch to American spelling.")

    [active] = facts.active_facts("me", "slack-dm")
    assert active.key == "spelling_preference"  # key normalized to match the old one
    assert active.text == "Use American spelling."
    [old] = facts.superseded_facts("me", "slack-dm", ["spelling_preference"])
    assert old.text == "Use British spelling."
    assert old.superseded_by == active.id

    profile = facts.render_profile("me", "other-session")
    assert profile.index("Use American spelling.") < profile.index("earlier: Use British")


def test_restating_a_fact_is_a_noop():
    with extractor_returning([[MANAGER]]):
        core.send("me", "A", "My manager is Priya Raman.")
        core.send("me", "A", "Reminder: my manager is Priya Raman.")
    assert len(facts.active_facts("me", "A")) == 1
    assert facts.superseded_facts("me", "A", ["manager"]) == []


def test_ephemeral_is_not_stored_and_thread_scope_stays_local():
    seen: list[str] = []
    with chat_with(seen):
        with extractor_returning([[PLAYFUL, TERSE]]):
            core.send("me", "A", "In this chat keep it terse. For this draft only, be playful.")
        core.send("me", "B", "Write a draft.")
        core.send("me", "A", "Another one.")

    stored = facts.active_facts("me", "A")
    assert [(f.scope, f.key) for f in stored] == [(Scope.THREAD, "reply_style")]
    assert "playful" not in seen[1].lower() and "terse" not in seen[1].lower()  # session B
    assert "terse" in seen[2].lower()  # back in session A


def test_ingest_extracts_in_batches_and_flush_finishes():
    seen_calls: list = []
    batch = get_settings().extract_batch_turns
    with extractor_returning([[]], seen_calls):
        for i in range(batch + 3):
            core.ingest("me", "slack-dm", f"message {i}", reply="ok")
        assert len(seen_calls) == 1  # one call for the first full batch
        assert store.count_pending_extraction("slack-dm") == 3
        core.flush("me", "slack-dm")
    assert len(seen_calls) == 2
    assert store.count_pending_extraction("slack-dm") == 0


def test_extraction_failure_does_not_lose_the_reply():
    def boom(messages, info):
        raise RuntimeError("extractor down")

    with get_extractor_override(FunctionModel(boom)):
        reply = core.send("me", "A", "My manager is Priya Raman.")
    assert reply  # chat reply survived
    assert store.count_pending_extraction("A") == 1  # retried next turn


def get_extractor_override(raw):
    from chatbot.memory.extract import get_extractor

    return get_extractor().override(model=build_model(get_settings(), raw=raw))


def test_commentary_preamble_is_not_part_of_the_reply():
    def fn(messages, info):
        return ModelResponse(
            parts=[
                TextPart("Let me check that first.", provider_details={"phase": "commentary"}),
                TextPart(
                    "Your manager is Priya Raman.", provider_details={"phase": "final_answer"}
                ),
            ]
        )

    with get_agent().override(model=build_model(get_settings(), raw=FunctionModel(fn))):
        assert core.send("me", "A", "Who's my manager?") == "Your manager is Priya Raman."
