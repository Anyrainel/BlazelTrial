"""Supervise live evaluations and clean up automatically allocated state."""

import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp

from scenarios.cli import parse_args

REPO = Path(__file__).resolve().parents[1]


def run_worker(state: Path, argv: list[str]) -> int:
    # A separate process releases SQLite and embedding handles before Windows cleanup.
    env = os.environ.copy()
    env["STATE_DIR"] = str(state)
    env["DOCS_DIR"] = str(state / "docs")
    return subprocess.run(
        [sys.executable, "-m", "scenarios.runner", *argv, "--state-dir", str(state)],
        env=env,
        cwd=REPO,
        check=False,
    ).returncode


def stop_owned_server(state: Path) -> None:
    """An autostarted model server can otherwise keep the temporary log open."""
    if (state / "local_server.pid").exists():
        from chatbot.agent.local_server import stop
        from chatbot.config import Settings

        # Only this fresh root's pid file can identify a server owned by this run.
        stop(Settings(state_dir=state))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parse_args(argv)
    if args.state_dir:
        state = Path(args.state_dir).resolve()
        print(f"Preserving explicit state directory: {state}", flush=True)
        return run_worker(state, argv)

    parent = REPO / ".state" / "scenarios"
    parent.mkdir(parents=True, exist_ok=True)
    if args.keep_state:
        state = Path(mkdtemp(prefix="run-", dir=parent))
        print(f"Keeping evaluation state: {state}", flush=True)
        return run_worker(state, argv)

    with TemporaryDirectory(prefix="run-", dir=parent) as temporary:
        state = Path(temporary)
        print("Temporary evaluation state will be removed on exit (use --keep-state to retain).")
        try:
            return run_worker(state, argv)
        finally:
            stop_owned_server(state)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
