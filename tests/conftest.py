import pytest
from pydantic_ai import models

from chatbot.agent.agent import get_agent
from chatbot.config import Settings, get_settings
from chatbot.memory.db import get_engine

# Unit tests must never reach a real provider.
models.ALLOW_MODEL_REQUESTS = False


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    """Fake key so the real model can be constructed (never called); state goes to tmp."""
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    _clear_caches()
    yield
    _clear_caches()


def _clear_caches() -> None:
    if get_engine.cache_info().currsize:
        get_engine().dispose()
    for fn in (get_settings, get_agent, get_engine):
        fn.cache_clear()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(_env_file=None, state_dir=tmp_path / "state", max_input_tokens=8000)
