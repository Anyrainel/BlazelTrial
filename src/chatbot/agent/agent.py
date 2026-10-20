"""The PydanticAI chat agent used by `core.send()`."""

from dataclasses import dataclass
from functools import cache

from pydantic_ai import Agent, RunContext
from pydantic_ai.agent import AgentRunResult
from pydantic_ai.messages import ModelResponse, TextPart

from chatbot import prompts
from chatbot.agent.models import build_model
from chatbot.config import get_settings
from chatbot.memory import facts


@dataclass(frozen=True)
class ChatDeps:
    user_id: str
    thread_id: str


@cache
def get_agent() -> Agent[ChatDeps, str]:
    """Build the chat agent once. Tests swap the model with `get_agent().override(model=...)`.

    Instruction order matters for prompt caching: the static system prompt first, then the
    profile (changes only when a fact changes). Recent turns follow as message history.
    """
    agent = Agent(
        build_model(get_settings()),
        deps_type=ChatDeps,
        instructions=prompts.load("system"),
    )

    @agent.instructions
    def profile(ctx: RunContext[ChatDeps]) -> str:
        return facts.render_profile(ctx.deps.user_id, ctx.deps.thread_id)

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
