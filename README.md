# Chatbot with shared memory under an 8k-token budget

One user, two surfaces: parallel **main-chat sessions** (CLI, HTTP, web) and one endless
**Slack DMs and channel @mentions**. Both call a single core, `send(user_id, thread_id, text) -> reply`. Memory is
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
  on first use if it is not running (`uv run chatbot local-server start|stop|status`). If it
  can't be reached or started, the stack uses `LLM_FALLBACK_PROVIDER`/`LLM_FALLBACK_MODEL`
  (default `anthropic`/`claude-sonnet-5-5`, needs `ANTHROPIC_API_KEY`; `none` to fail); or
  `LLM_PROVIDER=anthropic`, `LLM_MODEL=claude-sonnet-5-5` and `ANTHROPIC_API_KEY`; or
  `LLM_PROVIDER=meta`, `LLM_MODEL=muse-spark-1.3-contributor` and `META_API_KEY`.
- `MAX_INPUT_TOKENS` sets the per-call input budget (default 8000).
- All state lives in `STATE_DIR` (default `.state/`, gitignored): `state.db`, `docs/`,
  `logs/llm_calls.jsonl` and the embedding model cache.

The agent can discover document ids with `list_docs` (paginated), then use `fetch_doc`
or `search_doc` to read them. Generate the corpus (4 docs of 20k–40k tokens, incl. `Q3-plan`):

```bash
uv run python scripts/gen_docs.py
```

This creates `.state/docs/` on demand; the corpus is gitignored and must be generated
for each fresh checkout (or configured `STATE_DIR` / `DOCS_DIR`). The four synthetic
documents are `Q3-plan`, `brand-guidelines`, `eng-onboarding`, and `security-review`.
With the configured Bonsai tokenizer they contain roughly 25k–29k tokens each.
`Q3-plan` states Lumen's public launch date as **September 29, 2026**.
Character-based context estimates are conservative and are not document token counts.

Before submission, also:

- Run the offline tests and the full live scenario suite; inspect its saved
  `eval-results.json` and per-call usage log for failures or budget violations.
- Verify scenario 1 in the actual main chat and Slack DM, then record the required
  screen recording (at most two minutes). Configured tokens alone do not verify Slack setup.
- Update `NOTES.md` to reflect the final implementation/results and fill in hours spent.
- Package all unedited Claude Code and Codex transcripts separately, redacting API keys;
  exclude them and runtime state from Git. Submit the repository with its Git history.

## Run the core

One-shot call, the same entry point every surface uses (`thread_id` = a session id, `slack-dm`, or `slack-<channel ID>`):

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
3. **OAuth & Permissions** bot scopes: `chat:write`, `im:history`, `im:read`, `app_mentions:read`. Install to the
   workspace and copy the bot token into `SLACK_BOT_TOKEN` (`xoxb-…`).
4. **Event Subscriptions**: enable, and subscribe to bot events `message.im` and `app_mention`.
5. **App Home**: enable the Messages tab ("Allow users to send messages").
6. Invite Hazel to each desired channel with `/invite @Hazel` (including private channels).
7. Optional: `SLACK_ALLOWED_USER=U…` restricts both DMs and mentions to your Slack user id.
   Without it, all accepted authors share the same personal identity and memory.
8. When adding scopes to an existing app, reinstall it to grant them, then restart
   `chatbot slack`. Adding an event whose scope is already granted needs only Save Changes.

```bash
uv run chatbot slack
```

Reference: [Slack app_mention events](https://docs.slack.dev/reference/events/app_mention/).

DM the app: every DM goes to the `slack-dm` thread for the single configured user, so
shared long-term facts can be recalled from main chat, DMs, and channel conversations.
Mention `@Hazel` in a channel to chat. Each unique channel has one conversation
`slack-<channel ID>`; Slack reply threads share that channel context. Channel answers
are threaded under the request or its existing parent, without broadcast. Every
channel follow-up needs another mention. DM replies keep their incoming routing;
all DM reply threads still share `slack-dm`. Session records and draft-only rules
stay local; long-term facts are shared through the core's bounded memory tools.

Accepted DMs and mentions show a temporary “is thinking…” status while their
turn is processed, including delivery retries. Long requests refresh the status;
it is explicitly cleared after success, failure or cancellation. Status updates
use `assistant.threads.setStatus` with the existing `chat:write` scope; no reaction
scope is needed. Status API failures are logged without blocking answers.
Slack may render this as “Hazel is working” under the request or in its reply
thread. The startup log identifies the loaded source path and confirms status
support. After updating code, restart with `uv run chatbot slack` from this checkout;
an old `.state/runtime/` snapshot launcher will continue running its frozen code.

The app ignores ordinary channel messages, bot/self messages, edits and file-share
subtypes. No `message.channels`/`message.groups` subscriptions or history scopes are
required for mention-only handling. Existing broader subscriptions are harmless:
the adapter filters them before calling core. Run one Slack worker per state DB.
Turns are serialized per conversation through reply delivery. Generated replies
have persistent delivery receipts so a posting retry does not rerun tools. The
listener retries transient failures three times; exhausted retries are logged. There
is no background outbox drain after exhaustion or restart; replay of the original
event can retry pending delivery.
There remains a crash window between core completion and saving its receipt, and
between Slack accepting a post and confirming it locally; exactly-once delivery
across those crashes is not guaranteed. Pending replies deleted by forget are
suppressed. See [Slack evaluation](docs/SLACK_EVALUATION.md) for live evidence.

## Run the scenario script

Runs the 7 assignment scenarios against the real core and real model, in a fresh state dir
under `.state/scenarios/`. It loads 500 turns of synthetic Slack history (via `core.ingest`,
the same path as live messages) and prints PASS/FAIL per scenario, plus a check that every
call stayed within budget. It takes about 9 minutes and roughly 350 model calls.

Automatically allocated state is deleted after success, failure or Ctrl+C, once the
evaluation process has exited. Console results remain available. Use `--keep-state` to
retain a fresh run's database, logs and `eval-results.json` for inspection, or
`--state-dir PATH` to reuse and preserve a specific directory. Evaluation documents always
use that run's `docs/`, regardless of the application's `DOCS_DIR`. Forced termination or
power loss can bypass cleanup; remove those abandoned run directories manually.

```bash
uv run python -m scenarios
```

```bash
uv run python -m scenarios --only 1,4,5
```

```bash
uv run python -m scenarios --only 1,4,5 --keep-state
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

- **Budget guard** (`agent/budget.py`): every model is wrapped. Each request is counted before
  sending (Claude/Meta provider count endpoint; the local llama.cpp server's own template and
  tokenizer using the inference adapter's serialized payload). If counting fails, live
  inference is not sent. Estimates are only used to plan shrinking and in offline tests.
  One that is not below `MAX_INPUT_TOKENS` is shrunk (`agent/shrink.py`)
  and re-counted instead of failing; the reported usage is logged, and a low count raises the
  safety scale.
- **Context**: static instructions (system prompt + capability instructions) and a bounded
  recent-turn window, nothing else. A history hook re-fits every request of a turn. Long tool
  loops move older tool results behind `out:` refs and write a checkpoint.
- **Memory is tool-driven** (`capabilities/memory`):
  - One bounded memory worker, three modes. `ask_memory(question)` searches BM25 indexes of
    memory records, raw messages, episode notes and opened document passages (plus an exact
    `grep`) and returns a status, an answer and its sources.
  - `update_memory(change, scope)` lets the worker find the records a statement replaces and
    write a new version (the old one stays as history), save a new record, or retire one.
    Preference records are the one memory always injected ("Standing preferences" in the
    instructions), so they are short rules with a total size cap (`PREFERENCES_MAX_CHARS`,
    `SESSION_PREFERENCES_MAX_CHARS`); a write over the cap is refused until something is
    shortened, merged or retired.
  - `forget(what)` lets the worker find every wording, then remove it from active memory.
    Soft mode (default) never removes chat history: a message holding the forgotten value stays
    in the conversation as written, flagged `forgotten`, and memory never uses it again (not
    indexed, searched, read, replayed or summarized). Records, notes, saved outputs and sent
    messages derived from it are deleted for real. `MEMORY_FORGET_MODE=hard` also deletes the
    messages and purges the bytes.
- **Big documents** (`fetch_doc`, `search_doc`) are never inlined. They are read once by
  map-reduce into a cached note, with passages searchable.

## Context configuration

Context settings are centralized in `src/chatbot/config.py`, read from `.env` at startup:
`HISTORY_WINDOW_STEP=8`, `SUMMARY_CHUNK_TURNS=25`, `SUMMARY_KEEP_RECENT_TURNS=16`, and
`HISTORY_TOKEN_BUDGET=2500`. The first three mean window alignment, note segment size, and
how far behind the newest turn a segment must be before summarization. Restart the API and
Slack processes after changing settings. Existing notes remain; use a fresh state directory
when comparing different segment sizes.

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

## Inspect memory and artifacts

Open **Memory & artifacts** in the web sidebar to browse and search current/previous
memory records, their sources, conversation notes, document notes/passages and opened
documents, saved tool outputs, task checkpoints, mock messages, Slack delivery receipts
and legacy stores. Counts, pagination and refresh are available. Document filenames and
capability instructions are also visible. This is a read-only owner view across sessions;
it does not broaden the agent's own memory access. Forgotten chat text remains visible
in conversation history in soft mode; deleted derived memory is absent from this view.

Read-only APIs: `GET /agent/inventory`, `GET /agent/inventory/{category}?q=&offset=&limit=`.
`GET /agent/status` exposes budget/forget policy and documented delivery limitations for
programmatic queries. No delivery failure notification UI has been added. API keys and
runtime credentials are never included in these responses. Restart the API after pulling.
The artifact browser also shows system instructions and the last 500 usage-log entries
(filtered to the owner, with only usage fields exposed).

Evaluation coverage and limitations: see [docs/EVAL_REVIEW.md](docs/EVAL_REVIEW.md).
The default scenario runner now includes cases 8 (cross-session corrections) and 9
(event/amount distractors), requires real API counting/usage evidence, and saves
`eval-results.json` in its state directory (retained with `--keep-state` or `--state-dir`).
Run `uv run python -m scenarios --only 5,8,9`
for the strengthened locality check and added cases.
