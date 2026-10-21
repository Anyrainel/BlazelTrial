"""Command-line surface: one-shot `send` and an interactive `chat` loop."""

import json
import uuid
from datetime import datetime
from pathlib import Path

import typer

from chatbot.config import get_settings
from chatbot.core import SLACK_THREAD_ID
from chatbot.core import flush as core_flush
from chatbot.core import ingest as core_ingest
from chatbot.core import send as core_send

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.command()
def send(user_id: str, thread_id: str, text: str) -> None:
    """Call the core once and print the reply."""
    typer.echo(core_send(user_id, thread_id, text))


@app.command()
def chat(
    thread: str = typer.Option(None, help="Session id; a new one is generated if omitted."),
    user: str = typer.Option(None, help="User id; defaults to DEFAULT_USER_ID."),
) -> None:
    """Interactive session. Empty line or Ctrl+C to quit."""
    thread_id = thread or f"s-{uuid.uuid4().hex[:8]}"
    user_id = user or get_settings().default_user_id
    typer.echo(f"[session {thread_id}]")
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            break
        typer.echo(f"bot> {core_send(user_id, thread_id, text)}")


@app.command()
def ingest(
    path: Path,
    thread: str = typer.Option(SLACK_THREAD_ID, help="Thread to load the history into."),
    user: str = typer.Option(None, help="User id; defaults to DEFAULT_USER_ID."),
) -> None:
    """Load history from JSONL lines {"user", "assistant"?, "ts"?} via the core ingest path."""
    user_id = user or get_settings().default_user_id
    count = 0
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            at = datetime.fromisoformat(row["ts"]) if row.get("ts") else None
            core_ingest(user_id, thread, row["user"], row.get("assistant"), at)
            count += 1
    core_flush(user_id, thread)
    typer.echo(f"ingested {count} turns into {thread}")


@app.command()
def capabilities() -> None:
    """List capabilities and the tokens they add to every chat request (estimated)."""
    from chatbot.capabilities import registry

    total = 0
    for row in registry.cost_report():
        cost = row["instructions_tokens"] + row["schema_tokens"]
        total += cost
        typer.echo(
            f"{row['name']:<12} instructions ~{row['instructions_tokens']:>4}  "
            f"tools ~{row['schema_tokens']:>4}  total ~{cost:>4}"
        )
    typer.echo(f"{'all':<12} ~{total} tokens per chat request")


@app.command("migrate-memory")
def migrate_memory() -> None:
    """Copy legacy facts into versioned memory records (idempotent; safe to rerun)."""
    from chatbot.memory.migrate import migrate_facts

    typer.echo(migrate_facts())


@app.command("reindex")
def reindex() -> None:
    """Index any messages or memory records missing from the search indexes."""
    from chatbot.memory.search import reindex_missing

    typer.echo(reindex_missing())
