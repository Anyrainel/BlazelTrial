"""Evaluation cleanup must never delete user-selected or live application state."""

import subprocess
import sys
from pathlib import Path

import pytest

from scenarios import __main__ as supervisor


@pytest.fixture
def worker(monkeypatch, tmp_path):
    monkeypatch.setattr(supervisor, "REPO", tmp_path)
    actual_run = subprocess.run
    roots: list[Path] = []

    def run(command, **kwargs):
        state = Path(kwargs["env"]["STATE_DIR"])
        roots.append(state)
        assert kwargs["env"]["DOCS_DIR"] == str(state / "docs")
        # Real child process holds a database until exit, as the evaluation does.
        script = (
            "import os, sqlite3, pathlib, sys; "
            "root = pathlib.Path(os.environ['STATE_DIR']); "
            "root.mkdir(parents=True, exist_ok=True); "
            "db = sqlite3.connect(root / 'state.db'); "
            "db.execute('create table if not exists evidence (value text)'); "
            "(root / 'eval-results.json').write_text('{}'); "
            "sys.exit(int(os.environ.get('EVAL_TEST_EXIT', '0')))"
        )
        return actual_run([sys.executable, "-c", script], **kwargs)

    monkeypatch.setattr(supervisor.subprocess, "run", run)
    return roots


@pytest.mark.parametrize("exit_code", [0, 1])
def test_default_cleans_child_state_on_success_and_failure(
    worker, monkeypatch, tmp_path, exit_code
):
    live = tmp_path / ".state" / "state.db"
    live.parent.mkdir()
    live.write_text("live application data")
    monkeypatch.setenv("STATE_DIR", str(live.parent))
    monkeypatch.setenv("DOCS_DIR", str(tmp_path / "live-docs"))
    monkeypatch.setenv("EVAL_TEST_EXIT", str(exit_code))

    assert supervisor.main(["--only", "1"]) == exit_code
    assert len(worker) == 1
    assert not worker[0].exists()
    assert live.read_text() == "live application data"


def test_keep_state_allocates_unique_preserved_roots(worker):
    for _ in range(2):
        assert supervisor.main(["--only", "1", "--keep-state"]) == 0
    assert worker[0] != worker[1]
    assert all((root / "eval-results.json").exists() for root in worker)


def test_explicit_state_is_preserved_even_on_failure(worker, monkeypatch, tmp_path):
    state = tmp_path / "selected"
    state.mkdir()
    (state / "existing.txt").write_text("keep me")
    monkeypatch.setenv("EVAL_TEST_EXIT", "1")
    assert supervisor.main(["--only", "1", "--state-dir", str(state)]) == 1
    assert worker == [state]
    assert (state / "existing.txt").read_text() == "keep me"
    assert (state / "eval-results.json").exists()


@pytest.mark.parametrize("error", [KeyboardInterrupt, RuntimeError])
def test_interruption_or_launch_error_cleans_state(monkeypatch, tmp_path, error):
    monkeypatch.setattr(supervisor, "REPO", tmp_path)
    roots = []

    def interrupted(state, argv):
        roots.append(state)
        (state / "partial.txt").write_text("partial result")
        raise error()

    monkeypatch.setattr(supervisor, "run_worker", interrupted)
    with pytest.raises(error):
        supervisor.main(["--only", "1"])
    assert roots and not roots[0].exists()


def test_invalid_selection_allocates_no_state(worker, tmp_path):
    with pytest.raises(SystemExit):
        supervisor.main(["--only", "10"])
    assert not worker
    assert not (tmp_path / ".state").exists()


def test_stops_only_server_owned_by_temporary_root(monkeypatch, tmp_path):
    from chatbot.agent import local_server

    monkeypatch.setattr(supervisor, "REPO", tmp_path)
    stopped = []

    def run(state, argv):
        (state / "local_server.pid").write_text("123")
        return 0

    monkeypatch.setattr(supervisor, "run_worker", run)
    monkeypatch.setattr(local_server, "stop", lambda settings: stopped.append(settings.state_path))
    assert supervisor.main(["--only", "1"]) == 0
    assert len(stopped) == 1
    assert not stopped[0].exists()
