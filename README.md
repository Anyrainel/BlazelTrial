# Chatbot with shared memory under an 8k-token budget

One user, two surfaces: parallel **main-chat sessions** (CLI, HTTP, web) and one endless
**Slack DM**. Both call a single core, `send(user_id, thread_id, text) -> reply`. Memory is
persisted in SQLite and reached by the agent through bounded tools; **every model call stays
under `MAX_INPUT_TOKENS` (default 8,000)**, enforced before sending and logged per call.

Problem statement: [docs/PROBLEM.md](docs/PROBLEM.md) · design notes: [NOTES.md](NOTES.md) ·
decisions: [docs/DECISIONS.md](docs/DECISIONS.md) · memory contracts:
[docs/MEMORY_CONTRACTS.md](docs/MEMORY_CONTRACTS.md).

## Setup

Requires Python 3.13 with [uv](https://docs.astral.sh/uv/), and Node 20+ only for the web UI.

```bash
uv sync
```

```bash
cp .env.example .env
```

Then edit `.env`:
- Default: `LLM_PROVIDER=local`, `LLM_MODEL=bonsai-2-27b`, with `LOCAL_SERVER_BIN` (PrismML's
  llama.cpp fork) and `LOCAL_MODEL_PATH` (Bonsai 2 GGUF). The server on `LOCAL_BASE_URL` starts
  on first use if it is not running (`uv run chatbot local-server start|stop|status`); or
  `LLM_PROVIDER=anthropic`, `LLM_MODEL=claude-sonnet-5-5` and `ANTHROPIC_API_KEY`; or
  `LLM_PROVIDER=meta`, `LLM_MODEL=muse-spark-1.3-contributor` and `META_API_KEY`.
- `MAX_INPUT_TOKENS` sets the per-call input budget (default 8000).
- All state lives in `STATE_DIR` (default `.state/`, gitignored): `state.db`, `docs/`,
  `logs/llm_calls.jsonl` and the embedding model cache.

Generate the document corpus that `fetch_doc` reads (4 docs of 20k–40k tokens, incl. `Q3-plan`):

```bash
uv run python scripts/gen_docs.py
```

## Run the core

One-shot call, the same entry point every surface uses (`thread_id` = a session id or `slack-dm`):

```bash
uv run chatbot send me s1 "My manager is Priya Raman."
```

```bash
uv run chatbot send me slack-dm "Who's my manager?"
```

Interactive CLI session:

```bash
uv run chatbot chat --thread s2
```

HTTP (FastAPI) on http://127.0.0.1:8000. `POST /send {"thread_id", "text"}` returns
`{"reply"}`, and the web UI is served at `/` once built:

```bash
uv run chatbot serve
```

```bash
curl -s localhost:8000/send -H "Content-Type: application/json" -d "{\"thread_id\": \"s1\", \"text\": \"hi\"}"
```

Web UI (React + assistant-ui; build once, then `chatbot serve` serves it):

```bash
cd web && npm install && npm run build
```

Load synthetic Slack history through the same pipeline as live messages (JSONL lines
`{"user", "assistant"?, "ts"?}`):

```bash
uv run python -m scenarios.fixtures.slack_history .state/fixtures/slack_history.jsonl
```

```bash
uv run chatbot ingest .state/fixtures/slack_history.jsonl
```

Every model call is logged to `.state/logs/llm_calls.jsonl`, one JSON line per call:
`purpose`, `thread_id`, pre-flight estimate, provider-reported `input_tokens` (incl. cache
reads/writes), `within_budget`.

## Run the Slack app

1. Create an app at https://api.slack.com/apps ("From scratch") in a workspace you control.
2. **Socket Mode**: enable it and create an app-level token with `connections:write`. This is
   `SLACK_APP_TOKEN` (`xapp-…`). No public URL or tunnel is needed.
3. **OAuth & Permissions** bot scopes: `chat:write`, `im:history`, `im:read`. Install to the
   workspace and copy the bot token into `SLACK_BOT_TOKEN` (`xoxb-…`).
4. **Event Subscriptions**: enable, and subscribe to the bot event `message.im`.
5. **App Home**: enable the Messages tab ("Allow users to send messages").
6. Optional: `SLACK_ALLOWED_USER=U…` to answer only your own Slack user id.

```bash
uv run chatbot slack
```

DM the app: every DM goes to the `slack-dm` thread for the single configured user, so
something told in a main-chat session can be asked in Slack and vice versa.

## Run the scenario script

Runs the 7 assignment scenarios against the real core and real model, in a fresh state dir
under `.state/scenarios/`. It loads 500 turns of synthetic Slack history (via `core.ingest`,
the same path as live messages) and prints PASS/FAIL per scenario, plus a check that every
call stayed within budget. It takes about 9 minutes and roughly 350 model calls.

```bash
uv run python -m scenarios
```

```bash
uv run python -m scenarios --only 1,4,5
```

Offline unit tests (no network, fake models):

```bash
uv run pytest -q
```

Retrieval recall per search configuration (no LLM; point `STATE_DIR` at a state with history):

```bash
uv run python scripts/eval_retrieval.py
```

## How it works (short)

- **Budget guard** (`agent/budget.py`): every model is wrapped; pre-flight token count (provider
  endpoint, or a conservative estimate) refuses oversized requests before sending, and the
  reported usage is checked and logged.
- **Context**: static instructions (system prompt + capability instructions) and a bounded
  recent-turn window, nothing else. A history hook re-fits every request of a turn. Long tool
  loops move older tool results behind `out:` refs and write a checkpoint.
- **Memory is tool-driven** (`capabilities/memory`):
  - `ask_memory(question)` runs a bounded, read-only subagent over BM25 indexes of memory
    records, raw messages, episode notes and opened document passages. It returns a status,
    an answer and its sources.
  - `remember` / `update_memory` write versioned, source-linked records (session or
    long-term).
  - `forget` hard-deletes a value everywhere, down to the bytes on disk.
- **Big documents** (`fetch_doc`, `search_doc`) are never inlined. They are read once by
  map-reduce into a cached note, with passages searchable.

## Add a capability

Each capability is a folder under `src/chatbot/capabilities/`; it is discovered automatically
and loaded upfront.

1. Create `src/chatbot/capabilities/<name>/instructions.md`. It holds the short usage rules
   added to the chat agent's instructions.
2. Create `src/chatbot/capabilities/<name>/__init__.py` with the tool functions and a
   `CAPABILITY`:

```python
from pydantic_ai import RunContext
from chatbot.capabilities.registry import capability


def weather(ctx: RunContext, city: str) -> str:
    """Current weather for a city (the docstring is the tool description the model sees)."""
    return "..."  # keep tool output bounded; never return unbounded text


CAPABILITY = capability("weather", "Look up the weather.", __file__, weather)
```

3. Use `ctx.deps.user_id` / `ctx.deps.thread_id` for identity, never model arguments. If the
   tool mutates something, make it idempotent per turn (see `capabilities/memory.mutation_key`).
   If a model call is needed, build it with `agent.models.build_model` so it stays under the
   budget guard, and tag it with `call_context(purpose=...)`.
4. Check the per-request token cost, then add tests next to `tests/test_capabilities.py`:

```bash
uv run chatbot capabilities
```

Hard output rules belong in code, not prompts: see `capabilities/linkedin/validator.py`.
