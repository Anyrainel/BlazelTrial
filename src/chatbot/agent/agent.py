"""The PydanticAI chat agent used by `core.send()`."""

from dataclasses import dataclass
from functools import cache

from pydantic_ai import Agent, RunContext

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
