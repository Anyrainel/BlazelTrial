"""The PydanticAI agent used by `core.send()`."""

from functools import cache

from pydantic_ai import Agent

from chatbot import prompts
from chatbot.agent.models import build_model
from chatbot.config import get_settings


@cache
def get_agent() -> Agent[None, str]:
    """Build the chat agent once. Tests swap the model with `get_agent().override(model=...)`."""
    return Agent(build_model(get_settings()), instructions=prompts.load("system"))
