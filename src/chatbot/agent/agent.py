"""The PydanticAI chat agent used by `core.send()`."""

import json
from dataclasses import dataclass, field
from functools import cache

from pydantic_ai import Agent
from pydantic_ai.agent import AgentRunResult
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.messages import ModelResponse, TextPart

from chatbot import prompts
from chatbot.agent.history import fit_history
from chatbot.agent.models import build_model
from chatbot.capabilities import registry
from chatbot.config import get_settings


@dataclass(frozen=True)
class ChatDeps:
    """Trusted per-run context. Tools read identity from here, never from model arguments."""

    user_id: str
    thread_id: str
    current_msg_ref: str | None = None  # the user message being answered (citable source)
    # Set by tools during the run: a reply that must reach the user verbatim (e.g. a validated
    # LinkedIn post), replacing the model's own wording. Mutable on purpose.
    outputs: dict = field(default_factory=dict)


@cache
def get_agent() -> Agent[ChatDeps, str]:
    """Build the chat agent once. Tests swap the model with `get_agent().override(model=...)`.

    Instructions are static (system prompt + capability instructions), so they stay cached.
    Nothing about the user is injected: memory is reached through the memory capability's
    tools. Recent turns follow as message history.
    """
    agent = Agent(
        build_model(get_settings()),
        deps_type=ChatDeps,
        instructions=[prompts.load("system"), registry.instructions_block()],
        capabilities=[ProcessHistory(fit_history)],
        # Explicit breakpoints instead of automatic caching: one after tools + instructions,
        # one before the current message (see core._user_prompt). Other providers ignore these.
        model_settings={"anthropic_cache": False, "anthropic_cache_instructions": True},
    )
    registry.register_all(agent)
    return agent


def reply_text(result: AgentRunResult[str]) -> str:
    """The user-facing reply: final response text without progress preambles.

    Meta Muse models emit interim text tagged phase="commentary" ("I'll answer that next...");
    PydanticAI keeps it in the output string. We drop it here but keep it in the stored run,
    since the phase is sent back to the model on replay.
    """
    final = next((m for m in reversed(result.new_messages()) if isinstance(m, ModelResponse)), None)
    if final is None:
        return result.output
    texts = [
        p.content
        for p in final.parts
        if isinstance(p, TextPart) and (p.provider_details or {}).get("phase") != "commentary"
    ]
    return "\n\n".join(t.strip() for t in texts if t.strip()) or result.output


def tool_schema_tokens() -> int:
    """Estimated tokens of every tool definition sent with each chat request."""
    cpt = get_settings().estimate_chars_per_token
    tools = get_agent()._function_toolset.tools.values()
    chars = sum(
        len(json.dumps({"n": t.name, "d": t.description, "p": t.tool_def.parameters_json_schema}))
        for t in tools
    )
    return int(chars / cpt)


def static_prompt_tokens() -> int:
    """System prompt + capability instructions + tool schemas: sent with every chat request."""
    cpt = get_settings().estimate_chars_per_token
    static = prompts.load("system") + registry.instructions_block()
    return int(len(static) / cpt) + tool_schema_tokens()
