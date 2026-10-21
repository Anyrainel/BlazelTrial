"""The PydanticAI chat agent used by `core.send()`."""

from dataclasses import dataclass
from functools import cache

from pydantic_ai import Agent, RunContext
from pydantic_ai.agent import AgentRunResult
from pydantic_ai.messages import ModelResponse, TextPart

from chatbot import prompts
from chatbot.agent.models import build_model
from chatbot.capabilities import docs as doc_tools
from chatbot.config import get_settings
from chatbot.memory import retrieve


@dataclass(frozen=True)
class ChatDeps:
    user_id: str
    thread_id: str
    preferences: str = ""  # standing instructions (stable, cacheable)
    summaries: str = ""  # summary block of turns before the replayed window
    docs: str = ""  # documents opened in this thread


@cache
def get_agent() -> Agent[ChatDeps, str]:
    """Build the chat agent once. Tests swap the model with `get_agent().override(model=...)`.

    Instruction order matters for prompt caching: the static system prompt first, then standing
    preferences (change rarely), then summaries (change every chunk). Recent turns follow as
    message history; relevant facts and retrieved snippets ride in the current user message.
    """
    agent = Agent(
        build_model(get_settings()),
        deps_type=ChatDeps,
        instructions=prompts.load("system"),
        # Explicit breakpoints instead of automatic caching: one after tools + instructions,
        # one before the current message (see core._user_prompt). Automatic caching would also
        # write the volatile current message to cache on every call, which is never read back.
        # Other providers ignore these keys.
        model_settings={"anthropic_cache": False, "anthropic_cache_instructions": True},
    )

    @agent.instructions
    def preferences(ctx: RunContext[ChatDeps]) -> str:
        return ctx.deps.preferences

    @agent.instructions
    def summaries(ctx: RunContext[ChatDeps]) -> str:
        return ctx.deps.summaries

    @agent.instructions
    def docs(ctx: RunContext[ChatDeps]) -> str:
        return ctx.deps.docs

    doc_tools.register(agent)

    @agent.tool
    def search_history(ctx: RunContext[ChatDeps], query: str) -> str:
        """Search earlier messages of this conversation by keywords or meaning. Use it when the
        answer may be in an older part of the conversation not shown to you."""
        return retrieve.snippets(ctx.deps.thread_id, query) or "No matching earlier messages."

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
