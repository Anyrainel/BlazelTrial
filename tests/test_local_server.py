"""Local llama.cpp server autostart: reuse a running server, start one otherwise (offline)."""

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from chatbot.agent import local_server, models
from chatbot.config import Settings


class _Health(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = json.dumps({"status": "ok"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _serve_health(port: int) -> HTTPServer:
    server = HTTPServer(("127.0.0.1", port), _Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _settings(tmp_path, port: int, **kw) -> Settings:
    return Settings(
        _env_file=None,
        state_dir=tmp_path / "state",
        llm_provider="local",
        llm_model="bonsai-test",
        local_base_url=f"http://127.0.0.1:{port}/v1",
        local_start_timeout=5,
        **kw,
    )


def test_running_server_is_reused(tmp_path, monkeypatch):
    port = _free_port()
    server = _serve_health(port)
    monkeypatch.setattr(local_server, "_spawn", lambda s: pytest.fail("must not spawn"))
    try:
        assert local_server.ensure_running(_settings(tmp_path, port)) is False
    finally:
        server.shutdown()


def test_starts_server_when_nothing_listens(tmp_path, monkeypatch):
    port = _free_port()
    started = []

    def fake_spawn(settings):
        started.append(_serve_health(port))
        return SimpleNamespace(poll=lambda: None)  # a live Popen

    monkeypatch.setattr(local_server, "_spawn", fake_spawn)
    try:
        assert local_server.ensure_running(_settings(tmp_path, port)) is True
        assert local_server.ensure_running(_settings(tmp_path, port)) is False  # second call reuses
        assert len(started) == 1
    finally:
        for server in started:
            server.shutdown()


def test_missing_binary_is_a_clear_error(tmp_path):
    settings = _settings(tmp_path, _free_port(), local_server_bin=tmp_path / "nope.exe")
    with pytest.raises(RuntimeError, match="LOCAL_SERVER_BIN"):
        local_server.ensure_running(settings)


def test_remote_host_is_never_autostarted(tmp_path):
    settings = _settings(tmp_path, 1).model_copy(update={"local_base_url": "http://gpu.lan:80/v1"})
    with pytest.raises(RuntimeError, match="not local"):
        local_server.ensure_running(settings)


def test_command_serves_llm_model_name(tmp_path):
    settings = _settings(
        tmp_path, 1234, local_server_bin=tmp_path / "s", local_model_path=tmp_path / "m"
    )
    cmd = local_server.server_command(settings)
    assert cmd[cmd.index("--alias") + 1] == "bonsai-test"
    assert cmd[cmd.index("--port") + 1] == "1234"


def test_command_passes_reasoning_effort(tmp_path):
    settings = _settings(
        tmp_path, 1234, local_server_bin=tmp_path / "s", local_model_path=tmp_path / "m"
    )
    cmd = local_server.server_command(settings)
    assert cmd[cmd.index("--reasoning-effort") + 1] == "low"
    cmd = local_server.server_command(
        settings.model_copy(update={"local_reasoning_effort": "xhigh"})
    )
    assert cmd[cmd.index("--reasoning-effort") + 1] == "xhigh"


@pytest.fixture
def fresh_verdict(monkeypatch):
    monkeypatch.setattr(models, "_local_unavailable", {})


def test_factory_autostarts_local_when_enabled(tmp_path, monkeypatch, fresh_verdict):
    calls = []
    monkeypatch.setattr(local_server, "ensure_running", lambda s: calls.append(s))
    assert models.effective_settings(_settings(tmp_path, 1)).llm_provider == "local"
    assert len(calls) == 1
    models.effective_settings(_settings(tmp_path, 1))  # verdict cached: no second check
    assert len(calls) == 1


def _broken(settings):
    raise TimeoutError("not healthy")


def test_unavailable_local_falls_back(tmp_path, monkeypatch, fresh_verdict):
    monkeypatch.setattr(local_server, "ensure_running", _broken)
    settings = _settings(
        tmp_path, 1, llm_fallback_provider="anthropic", llm_fallback_model="claude-x"
    )
    chosen = models.effective_settings(settings)
    assert (chosen.llm_provider, chosen.llm_model) == ("anthropic", "claude-x")
    assert chosen.uses_server_count  # budget pre-flight follows the model actually used


def test_no_autostart_and_nothing_running_falls_back(tmp_path, fresh_verdict):
    settings = _settings(tmp_path, _free_port(), local_autostart=False)
    assert models.effective_settings(settings).llm_provider == "anthropic"


def test_fallback_none_raises(tmp_path, monkeypatch, fresh_verdict):
    monkeypatch.setattr(local_server, "ensure_running", _broken)
    with pytest.raises(RuntimeError, match="LLM_FALLBACK_PROVIDER=none"):
        models.effective_settings(_settings(tmp_path, 1, llm_fallback_provider="none"))


def test_stop_ignores_stale_pid(tmp_path, monkeypatch):
    settings = _settings(tmp_path, _free_port())
    settings.state_path.mkdir(parents=True)
    (settings.state_path / "local_server.pid").write_text("12345")
    monkeypatch.setattr(local_server.os, "kill", lambda *a: pytest.fail("must not signal"))
    assert local_server.stop(settings) is False
    assert not (settings.state_path / "local_server.pid").exists()


def test_mechanical_tasks_run_without_thinking_only_on_the_local_model(tmp_path, fresh_verdict):
    local = _settings(tmp_path, 1)
    models._local_unavailable[local.local_base_url] = None  # server is up
    out = models.mechanical_task_settings(local, max_tokens=900)
    assert out["max_tokens"] == 900
    assert out["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}
    claude = local.model_copy(update={"llm_provider": "anthropic"})
    assert models.mechanical_task_settings(claude, max_tokens=900) == {"anthropic_cache": False}


def test_mechanical_settings_can_keep_the_prompt_cache(tmp_path, fresh_verdict):
    claude = _settings(tmp_path, 1).model_copy(update={"llm_provider": "anthropic"})
    assert models.mechanical_task_settings(claude, max_tokens=900, cache=True) == {
        "anthropic_cache": True
    }
