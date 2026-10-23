"""Start/stop the local llama.cpp server (Bonsai 2) that backs `LLM_PROVIDER=local`.

One server is shared by every surface and process: it is started detached, so it outlives the
process that started it. The port is the single-instance guard: if something already answers
on it we reuse it, and a second llama-server racing for the same port fails to bind and exits.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse

from chatbot.config import Settings

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


@dataclass(frozen=True)
class ServerStatus:
    running: bool  # /health answered "ok"
    port_in_use: bool
    pid: int | None  # from the pid file, if we started it


def _endpoint(settings: Settings) -> tuple[str, int]:
    url = urlparse(settings.local_base_url)
    return url.hostname or "127.0.0.1", url.port or 80


def _healthy(host: str, port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=2) as resp:
            return json.load(resp).get("status") == "ok"
    except (OSError, ValueError):
        return False


def _port_in_use(host: str, port: int) -> bool:
    try:  # tries every address family: "localhost" may be bound on ::1 only
        socket.create_connection((host, port), timeout=1).close()
    except OSError:
        return False
    return True


def _pid_path(settings: Settings):
    return settings.state_path / "local_server.pid"


def _read_pid(settings: Settings) -> int | None:
    try:
        return int(_pid_path(settings).read_text())
    except (OSError, ValueError):
        return None


def status(settings: Settings) -> ServerStatus:
    host, port = _endpoint(settings)
    return ServerStatus(_healthy(host, port), _port_in_use(host, port), _read_pid(settings))


def server_command(settings: Settings) -> list[str]:
    """llama-server argv. Sampling follows the Bonsai 2 27B model card; --jinja gives
    OpenAI-style tool_calls; the alias makes LLM_MODEL the served model name."""
    host, port = _endpoint(settings)
    return [
        str(settings.local_server_bin),
        "-m", str(settings.local_model_path),
        "--alias", settings.llm_model,
        "-ngl", "99", "-fa", "on",
        "-c", str(settings.local_context), "-np", str(settings.local_parallel),
        "-n", "16384", "--reasoning", settings.local_reasoning,
        "--reasoning-effort", settings.local_reasoning_effort,
        "--jinja", "--temp", "1.0", "--top-p", "0.95", "--top-k", "20", "--min-p", "0.05",
        "--host", host, "--port", str(port),
    ]  # fmt: skip


def _spawn(settings: Settings) -> subprocess.Popen:
    for name, path in (
        ("LOCAL_SERVER_BIN", settings.local_server_bin),
        ("LOCAL_MODEL_PATH", settings.local_model_path),
    ):
        if path is None or not path.exists():
            raise RuntimeError(f"{name} is not set or missing ({path}); add it to .env")
    log_path = settings.state_path / "logs" / "local_server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    flags = 0
    if sys.platform == "win32":
        flags = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
    with log_path.open("ab") as log:
        proc = subprocess.Popen(
            server_command(settings),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=flags,
            start_new_session=sys.platform != "win32",
        )
    _pid_path(settings).write_text(str(proc.pid))
    return proc


def ensure_running(settings: Settings) -> bool:
    """Make sure the local server answers; start it if nothing listens. True if we started it."""
    host, port = _endpoint(settings)
    if host not in LOCAL_HOSTS:
        raise RuntimeError(f"LOCAL_BASE_URL host {host!r} is not local; cannot autostart it")
    if _healthy(host, port):
        return False
    proc = None if _port_in_use(host, port) else _spawn(settings)  # in use = still loading
    deadline = time.monotonic() + settings.local_start_timeout
    while time.monotonic() < deadline:
        if _healthy(host, port):
            return proc is not None
        if proc is not None and proc.poll() is not None and not _port_in_use(host, port):
            raise RuntimeError(
                f"llama-server exited with code {proc.returncode}; "
                f"see {settings.state_path / 'logs' / 'local_server.log'}"
            )
        time.sleep(0.5)
    raise TimeoutError(
        f"local server on port {port} not healthy after {settings.local_start_timeout}s"
    )


def stop(settings: Settings) -> bool:
    """Stop the server we started (by pid file). True if a process was signalled."""
    pid = _read_pid(settings)
    _pid_path(settings).unlink(missing_ok=True)
    # Nothing listening means the server is gone and the pid may belong to something else now.
    if pid is None or not _port_in_use(*_endpoint(settings)):
        return False
    try:
        os.kill(pid, signal.SIGTERM)  # TerminateProcess on Windows
    except OSError:
        return False
    return True
