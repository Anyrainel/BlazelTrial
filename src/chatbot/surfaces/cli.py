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


@app.command("memory-search")
def memory_search(
    session_id: str,
    query: str,
    sources: str = typer.Option(
        "records,messages,notes", help="Comma list: records,messages,notes,vectors"
    ),
    store: str = typer.Option("both", help="session | long_term | both"),
    k: int = typer.Option(5),
    user: str = typer.Option(None, help="User id; defaults to DEFAULT_USER_ID."),
) -> None:
    """Diagnostics: run the memory store's fused search as a given session would."""
    from chatbot.memory.records import MemoryCaller, search_memory

    caller = MemoryCaller(user or get_settings().default_user_id, session_id)
    hits = search_memory(caller, query, store=store, k=k, sources=tuple(sources.split(",")))
    for h in hits:
        extra = f" covers {h.covers}" if h.covers else (f" turn {h.turn}" if h.turn else "")
        typer.echo(f"{h.ref:<10} {h.type:<8} {'/'.join(h.found_by)}{extra}\n    {h.excerpt}")
    if not hits:
        typer.echo("(no hits)")


@app.command("local-server")
def local_server(action: str = typer.Argument("status", help="start | stop | status")) -> None:
    """Manage the llama.cpp server behind LLM_PROVIDER=local (also autostarted on first use)."""
    from chatbot.agent import local_server as server

    settings = get_settings()
    match action:
        case "start":
            started = server.ensure_running(settings)
            typer.echo(f"{'started' if started else 'already running'}: {settings.local_base_url}")
        case "stop":
            typer.echo("stopped" if server.stop(settings) else "no server started by us is running")
        case "status":
            typer.echo(server.status(settings))
        case _:
            raise typer.BadParameter("expected start, stop or status")


@app.command()
def serve(
    host: str = typer.Option(None, help="Defaults to HTTP_HOST."),
    port: int = typer.Option(None, help="Defaults to HTTP_PORT."),
) -> None:
    """HTTP API (POST /send, /threads) and, if built, the web UI at /."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "chatbot.surfaces.http:app",
        host=host or settings.http_host,
        port=port or settings.http_port,
    )


@app.command()
def slack() -> None:
    """Connect the Slack app (Socket Mode) and answer DMs as the `slack-dm` thread."""
    import logging

    from chatbot.surfaces.slack_app import run

    logging.basicConfig(level=logging.INFO)
    run()
