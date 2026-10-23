import random

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
from chatbot.capabilities import registry
from chatbot.capabilities.linkedin import BODY_LIMIT, get_writer, validator
from chatbot.capabilities.messaging import sent_messages
from chatbot.config import get_settings


def chat_calling(tool: str, args: dict, final_text: str = "Here you go!") -> FunctionModel:
    """Chat model: call `tool` once, then answer with `final_text`."""

    def fn(messages, info: AgentInfo) -> ModelResponse:
        done = any(
            isinstance(p, ToolReturnPart)
            for m in messages
            if isinstance(m, ModelRequest)
            for p in m.parts
        )
        if done:
            return ModelResponse(parts=[TextPart(final_text)])
        return ModelResponse(parts=[ToolCallPart(tool, args)])

    return FunctionModel(fn)


def writer_returning(*posts: dict) -> FunctionModel:
    """Writer stand-in: call N returns posts[N] (the last repeats)."""
    calls = {"n": 0}

    def fn(messages, info: AgentInfo) -> ModelResponse:
        post = posts[min(calls["n"], len(posts) - 1)]
        calls["n"] += 1
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, post)])

    model = FunctionModel(fn)
    model.calls = calls  # type: ignore[attr-defined]
    return model


# --- registry -------------------------------------------------------------------------------


def test_registry_discovers_capabilities_and_loads_them_upfront():
    names = [c.name for c in registry.all_capabilities()]
    assert names == sorted(names) and {"documents", "linkedin", "messaging"} <= set(names)
    tools = set(get_agent()._function_toolset.tools)
    assert {"list_docs", "fetch_doc", "search_doc", "send_message", "write_linkedin_post"} <= tools
    block = registry.instructions_block()
    assert all(f"### {n}" in block for n in names)
    assert all(
        r["instructions_tokens"] > 0 and r["schema_tokens"] > 0 for r in registry.cost_report()
    )


def test_capability_instructions_reach_the_model():
    seen: list[str] = []

    def fn(messages, info):
        seen.append(messages[-1].instructions or "")
        return ModelResponse(parts=[TextPart("ok")])

    with get_agent().override(model=build_model(get_settings(), raw=FunctionModel(fn))):
        core.send("me", "A", "hi")
    assert "## Capabilities" in seen[0] and "write_linkedin_post" in seen[0]


# --- LinkedIn rules -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "post, problem",
    [
        ("x" * 1301, "characters"),
        ("Great news!\n\n#a #b #c #d", "hashtags (max"),
        ("We #launch today.\n\n#Lumen", "at the end"),
        ("#Lumen\n\nWe launch today.", "at the end"),
        ("See https://example.com for more.", "link"),
        ("Visit www.lumen.ai today.", "link"),
        ("Details at lumen.ai/launch.", "link"),
    ],
)
def test_problems_catches_each_rule(post, problem):
    assert any(problem in p for p in validator.problems(post))


def test_valid_post_has_no_problems():
    post = "We launch Lumen on September 29, 2026. Version 2.0 is ready.\n\n#Lumen #AI #Launch"
    assert validator.problems(post) == []


def test_assemble_always_produces_a_valid_post():
    rng = random.Random(3)
    words = (
        "launch Lumen meeting notes team #growth https://x.io/a www.lumen.ai lumen.com 2.0".split()
    )
    for _ in range(300):
        body = " ".join(rng.choice(words) for _ in range(rng.randint(1, 600)))
        tags = [rng.choice(["AI", "#Launch", "Lumen", "lumen", "Q3!", "", "#"]) for _ in range(6)]
        post = validator.assemble(body, tags)
        assert validator.problems(post) == [], post


def test_assemble_keeps_numbers_and_trims_on_a_sentence():
    body = "Lumen 2.0 launches September 29, 2026. " * 60
    post = validator.assemble(body, ["Lumen"])
    assert len(post) <= 1300 and post.endswith("#Lumen")
    assert post.split("\n\n")[0].endswith(".")
    assert "2.0" in post and "$1.2M" in validator.assemble("Budget is $1.2M.", [])


# --- LinkedIn tool end to end ------------------------------------------------------------------


def test_linkedin_reply_is_the_validated_post_not_the_models_wording():
    messy = {
        "body": "Big news: Lumen launches Sept 29! #excited Read more at https://lumen.ai " * 30,
        "hashtags": ["Lumen", "AI", "Launch"],
    }
    chat = chat_calling(
        "write_linkedin_post",
        {"brief": "Announce Lumen launch on September 29, 2026"},
        final_text="Here's your post with a link: https://lumen.ai #one #two #three #four",
    )
    writer = writer_returning(messy, messy, messy)  # keeps failing the length check
    settings = get_settings()
    with (
        get_agent().override(model=build_model(settings, raw=chat)),
        get_writer().override(model=build_model(settings, raw=writer)),
    ):
        reply = core.send("me", "slack-dm", "Write a LinkedIn post announcing our Q3 launch.")

    assert validator.problems(reply) == []
    assert reply.endswith("#Lumen #AI #Launch")
    assert "http" not in reply and "#excited" not in reply


def test_writer_gets_one_retry_with_the_overrun():
    long = {"body": "word " * 400, "hashtags": ["Lumen"]}
    short = {"body": "Lumen launches on September 29, 2026.", "hashtags": ["Lumen"]}
    writer = writer_returning(long, short)
    chat = chat_calling("write_linkedin_post", {"brief": "Lumen launch"})
    settings = get_settings()
    with (
        get_agent().override(model=build_model(settings, raw=chat)),
        get_writer().override(model=build_model(settings, raw=writer)),
    ):
        reply = core.send("me", "A", "post please")
    assert writer.calls["n"] == 2
    assert reply == "Lumen launches on September 29, 2026.\n\n#Lumen"
    assert BODY_LIMIT < 1300


def test_writer_gets_preferences_through_the_brief():
    prompts: list[str] = []

    def fn(messages, info):
        prompts.append(str(messages[-1].parts[-1].content))
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"body": "Colour!", "hashtags": []})]
        )

    settings = get_settings()
    brief = {"brief": "Launch post. Writing preferences: use British spelling."}
    with (
        get_agent().override(
            model=build_model(settings, raw=chat_calling("write_linkedin_post", brief))
        ),
        get_writer().override(model=build_model(settings, raw=FunctionModel(fn))),
    ):
        core.send("me", "A", "post")
    assert "British spelling" in prompts[0]


# --- messaging ---------------------------------------------------------------------------------


def test_send_message_is_recorded_not_sent():
    chat = chat_calling("send_message", {"to": "Sam", "text": "Running late, home by 7."})
    with get_agent().override(model=build_model(get_settings(), raw=chat)):
        core.send("me", "slack-dm", "Tell my partner I'm running late")
    [msg] = sent_messages("slack-dm")
    assert (msg.recipient, msg.text) == ("Sam", "Running late, home by 7.")


def test_send_message_refuses_empty_input():
    chat = chat_calling("send_message", {"to": " ", "text": "hi"})
    with get_agent().override(model=build_model(get_settings(), raw=chat)):
        core.send("me", "A", "message nobody")
    assert sent_messages() == []
