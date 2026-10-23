"""The PydanticAI chat agent used by `core.send()`."""

import json
from dataclasses import dataclass, field
from functools import cache

from pydantic_ai import Agent, RunContext
from pydantic_ai.agent import AgentRunResult
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.messages import ModelResponse, TextPart

from chatbot import prompts
from chatbot.agent.history import fit_history
from chatbot.agent.models import build_model
from chatbot.capabilities import registry
from chatbot.config import get_settings
from chatbot.memory import records
from chatbot.memory.records import MemoryCaller


@dataclass(frozen=True)
class ChatDeps:
    """Trusted per-run context. Tools read identity from here, never from model arguments."""

    user_id: str
    thread_id: str
    current_msg_ref: str | None = None  # the user message being answered (citable source)
    # Set by tools during the run: a reply that must reach the user verbatim (e.g. a validated
    # LinkedIn post), replacing the model's own wording. Mutable on purpose.
    outputs: dict = field(default_factory=dict)


def preference_block(user_id: str, thread_id: str) -> str:
    """The standing preferences, the one memory injected into every request (size-capped)."""
    views = records.current_preferences(MemoryCaller(user_id, thread_id))
    if not views:
        return ""
    lines = [
        f"- {v.text}" + (" (this conversation only)" if v.store == "session" else "") for v in views
    ]
    intro = (
        "## Standing preferences\n"
        "The user's rules for how you write and behave. Follow them in everything you do; "
        "they override your defaults, and they apply to wording you copy from the user's "
        "request or earlier messages too. Change them only through `update_memory`.\n"
    )
    return intro + "\n".join(lines)


def chat_instructions(ctx: RunContext[ChatDeps]) -> str:
    """The whole system text as ONE instruction: static parts first, preferences last (the only
    part that ever changes). Separate instruction parts become separate system messages, which
    chat templates such as Bonsai's reject ("System message must be at the beginning")."""
    parts = [prompts.load("system"), registry.instructions_block()]
    parts.append(preference_block(ctx.deps.user_id, ctx.deps.thread_id))
    parts.append(f"Today's date: {records.today()}.")  # last: it changes only once a day
    return "\n\n".join(p for p in parts if p)


def preference_reserve_tokens() -> int:
    """Worst-case size of the preference block (and the date line), reserved when sizing the
    rest of the context."""
    settings = get_settings()
    chars = settings.preferences_max_chars + settings.session_preferences_max_chars
    return int(chars / settings.estimate_chars_per_token) + 80


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
        instructions=chat_instructions,
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
    return int(len(static) / cpt) + tool_schema_tokens() + preference_reserve_tokens()
