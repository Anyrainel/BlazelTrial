import pytest
from pydantic_ai import models

from chatbot.agent.agent import get_agent
from chatbot.config import Settings, get_settings
from chatbot.memory.db import get_engine
from chatbot.memory.embeddings import get_embedder
from chatbot.memory.extract import get_extractor
from chatbot.memory.search import _ensure_vec
from chatbot.memory.summarize import get_summarizer

# Unit tests must never reach a real provider.
models.ALLOW_MODEL_REQUESTS = False


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    """Fake key so the real model can be constructed (never called); state goes to tmp."""
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EMBEDDING_BACKEND", "hash")
    _clear_caches()
    yield
    _clear_caches()


def _clear_caches() -> None:
    if get_engine.cache_info().currsize:
        get_engine().dispose()
    for fn in (
        get_settings,
        get_agent,
        get_engine,
        get_extractor,
        get_embedder,
        get_summarizer,
        _ensure_vec,
    ):
        fn.cache_clear()


@pytest.fixture(autouse=True)
def _fake_models(_isolated_env):
    """Default offline models: chat replies "ok", the extractor remembers nothing, summaries
    are a fixed string.
    Tests that care override them again (inner overrides win)."""
    from pydantic_ai.models.test import TestModel

    from chatbot.agent.models import build_model
    from tests.fakes import extractor_returning

    settings = get_settings()
    chat = build_model(settings, raw=TestModel(custom_output_text="ok", call_tools=[]))
    summary = build_model(settings, raw=TestModel(custom_output_text="summary"))
    with (
        get_agent().override(model=chat),
        get_summarizer().override(model=summary),
        extractor_returning([]),
    ):
        yield


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(_env_file=None, state_dir=tmp_path / "state", max_input_tokens=8000)
