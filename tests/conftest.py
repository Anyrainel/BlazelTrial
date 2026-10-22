import pytest
from pydantic_ai import models

from chatbot.agent.agent import get_agent
from chatbot.capabilities.linkedin import get_writer
from chatbot.capabilities.memory.subagent import Mode, get_worker
from chatbot.config import Settings, get_settings
from chatbot.docs_reader.reader import get_mapper, get_reducer
from chatbot.memory.db import get_engine
from chatbot.memory.embeddings import get_embedder
from chatbot.memory.notes import get_note_writer
from chatbot.memory.search import _ensure_doc_vec, _ensure_vec, repair_once

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
        get_embedder,
        get_note_writer,
        get_worker,
        _ensure_vec,
        _ensure_doc_vec,
        get_mapper,
        get_reducer,
        get_writer,
        repair_once,
    ):
        fn.cache_clear()


@pytest.fixture(autouse=True)
def _fake_models(_isolated_env):
    """Default offline models: chat replies "ok", notes and doc notes are a fixed string,
    the memory search answers not_found.
    Tests that care override them again (inner overrides win)."""
    from pydantic_ai.models.test import TestModel

    from chatbot.agent.models import build_model
    from tests.fakes import search_agent_answering, worker_answering

    settings = get_settings()
    chat = build_model(settings, raw=TestModel(custom_output_text="ok", call_tools=[]))
    summary = build_model(settings, raw=TestModel(custom_output_text="summary"))
    with (
        get_agent().override(model=chat),
        get_note_writer().override(model=summary),
        get_mapper().override(model=summary),
        get_reducer().override(model=summary),
        search_agent_answering({"status": "not_found", "answer": "Nothing found."}),
        worker_answering(Mode.WRITE, {"status": "skipped", "summary": "Nothing stored."}),
        worker_answering(Mode.FORGET, {"status": "nothing_found", "summary": "Nothing found."}),
    ):
        yield


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(_env_file=None, state_dir=tmp_path / "state", max_input_tokens=8000)
