import pytest
from pydantic_ai import models

from chatbot.agent.agent import get_agent
from chatbot.config import Settings, get_settings
from chatbot.memory.db import get_engine
from chatbot.memory.extract import get_extractor

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
    for fn in (get_settings, get_agent, get_engine, get_extractor):
        fn.cache_clear()


@pytest.fixture(autouse=True)
def _fake_models(_isolated_env):
    """Default offline models: chat replies "ok", the extractor remembers nothing.
    Tests that care override them again (inner overrides win)."""
    from pydantic_ai.models.test import TestModel

    from chatbot.agent.models import build_model
    from tests.fakes import extractor_returning

    chat = build_model(get_settings(), raw=TestModel(custom_output_text="ok"))
    with get_agent().override(model=chat), extractor_returning([]):
        yield


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(_env_file=None, state_dir=tmp_path / "state", max_input_tokens=8000)
