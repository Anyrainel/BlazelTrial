# Hazel

A personal chatbot with shared memory across web, CLI and Slack conversations.

## Get running

Requires Python 3.13 and [uv](https://docs.astral.sh/uv/).
For the web UI, also install Node.js 22.12+ and npm.
Run these commands from the repository root.

### 1. Install and configure

```sh
uv sync
cp .env.example .env
```

In PowerShell, use `Copy-Item .env.example .env` to copy the configuration.

For a quick start with Claude, set these values in `.env`:

```dotenv
LLM_PROVIDER=anthropic
LLM_MODEL=claude-sonnet-5-5
ANTHROPIC_API_KEY=your-key
```

The example file defaults to a local model; the values above switch to Claude.
Local and Meta options are listed below.

### 2. Start chatting

For the web UI, build it once:

```sh
cd web
npm ci
npm run build
cd ..
uv run chatbot serve
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000).
Rebuild the frontend after pulling frontend changes.

Or start an interactive CLI session:

```sh
uv run chatbot chat --thread s1
```

Send an empty line or press Ctrl+C to exit. Reuse `s1` to continue that conversation,
or choose another thread name to start a separate one.

For a single reply:

```sh
uv run chatbot send me s1 "My manager is Priya Raman."
```

## Environment variables

Settings are read from `.env` at startup. Restart the web/API and Slack processes after
changing them. Keep API keys and Slack tokens in your private `.env`.

### Model and storage

| Variable | Purpose |
|---|---|
| `LLM_PROVIDER` | `anthropic`, `meta` or `local`. The example defaults to `local`. |
| `LLM_MODEL` | Model name for the selected provider. |
| `ANTHROPIC_API_KEY` | Required for Claude, including when used as the local-model fallback. |
| `META_API_KEY` | Required for Meta. |
| `MAX_INPUT_TOKENS` | Per-call input-token limit; default `8000`. |
| `STATE_DIR` | Runtime folder; default `.state/`. |
| `DOCS_DIR` | Documents Hazel can read; defaults to `STATE_DIR/docs/`. |
| `DEFAULT_USER_ID` | Shared user identity for web, CLI and Slack; default `me`. |
| `HTTP_HOST`, `HTTP_PORT` | API/web address; defaults to `127.0.0.1:8000`. |

By default, runtime files live in `.state/`: the database, usage logs, model cache and
Hazel's documents. Hazel reads `.state/docs/`; repository-root `docs/` contains development
documentation. Model-call usage is recorded in `.state/logs/llm_calls.jsonl`.

### Slack

| Variable | Purpose |
|---|---|
| `SLACK_BOT_TOKEN` | Bot token (`xoxb-…`). |
| `SLACK_APP_TOKEN` | App-level Socket Mode token (`xapp-…`). |
| `SLACK_ALLOWED_USER` | Optional Slack user ID (`U…`) allowed to chat. Without it, accepted users share the same identity and memory. |

### Local model

| Variable | Purpose |
|---|---|
| `LOCAL_BASE_URL` | Server API address; default `http://localhost:11434/v1`. |
| `LOCAL_SERVER_BIN` | Path to the llama.cpp server executable. Bonsai 2 requires PrismML's fork. |
| `LOCAL_MODEL_PATH` | Path to the Bonsai 2 GGUF model file. Replace the example's machine-specific paths. |
| `LOCAL_AUTOSTART` | Start the server on first use; default `true`. |
| `LLM_FALLBACK_PROVIDER` | Provider used if the local server is unavailable; default `anthropic`. Set `none` to disable fallback. |
| `LLM_FALLBACK_MODEL` | Fallback model; default `claude-sonnet-5-5`. |

## Optional setup and commands

### Use another model

For Meta, set `LLM_PROVIDER=meta`, `LLM_MODEL=muse-spark-1.3-contributor` and
`META_API_KEY` in `.env`.

For local Bonsai 2, set `LLM_PROVIDER=local`, `LLM_MODEL=bonsai-2-27b`,
`LOCAL_SERVER_BIN` and `LOCAL_MODEL_PATH`. To use an already running server, set
`LOCAL_BASE_URL` and `LOCAL_AUTOSTART=false`. The server must support template and token-count
endpoints. Live inference stops if token counting is unavailable.

Manual local-server controls:

```sh
uv run chatbot local-server start
uv run chatbot local-server status
uv run chatbot local-server stop
```

### Connect Slack

1. Create an app at [Slack Apps](https://api.slack.com/apps) in your workspace.
2. Enable **Socket Mode**. Create an app-level token with `connections:write` and put it in
   `SLACK_APP_TOKEN`.
3. Under **OAuth & Permissions**, add bot scopes `chat:write`, `im:history`, `im:read` and
   `app_mentions:read`. Install the app and put its bot token in `SLACK_BOT_TOKEN`.
4. Enable **Event Subscriptions** and subscribe to `message.im` and `app_mention`.
5. Under **App Home**, enable the Messages tab and allow users to send messages.
6. Invite the app to channels where you want to use it: `/invite @Hazel`.

Start the worker in a separate terminal:

```sh
uv run chatbot slack
```

DM Hazel or mention `@Hazel` in a channel. Every channel follow-up needs another mention.
DMs share one conversation; each channel has its own conversation. Run one Slack worker
per state database. After changing bot scopes, reinstall the app and restart the worker.

### Documents and history

Generate the sample documents if needed, including `Q3-plan`:

```sh
uv run python scripts/gen_docs.py
```

Documents are written to `DOCS_DIR`, or `.state/docs/` by default.

Import existing history from JSONL with one `{"user":"…","assistant":"…"}` object per line
(`assistant` is optional):

```sh
uv run chatbot ingest history.jsonl --thread slack-dm
```

### API and frontend development

`uv run chatbot serve` also exposes `POST /send`. Send a JSON body such as
`{"thread_id":"s1","text":"Hello"}`; the response contains `{"reply":"…"}`.

Override the listening address:

```sh
uv run chatbot serve --host 127.0.0.1 --port 8080
```

For frontend development, run the API on port 8000, then run `npm run dev` from `web/`
and open the URL it prints.

### Memory and context options

These settings can be added to `.env`; the defaults are suitable for getting started.

| Variable | Default | Purpose |
|---|---|---|
| `MEMORY_FORGET_MODE` | `soft` | Preserve visible chat when forgetting. `hard` also deletes matching chat messages. |
| `HISTORY_TOKEN_BUDGET` | `2500` | Recent-history token allowance. |
| `HISTORY_WINDOW_STEP` | `8` | Turns between history-window boundaries. |
| `SUMMARY_CHUNK_TURNS` | `25` | Conversation segment size for notes. |
| `SUMMARY_KEEP_RECENT_TURNS` | `16` | How many recent turns to leave before generating notes. |
| `PREFERENCES_MAX_CHARS` | `1200` | Total length allowed for shared standing preferences. |
| `SESSION_PREFERENCES_MAX_CHARS` | `400` | Total length allowed for session preferences. |
| `MEMORY_ANTHROPIC_THINKING` | `between_tools` in `.env.example` | Use `between_tools` for Sonnet 5.5 to disable pre-response thinking; older Claude models use `disabled`. |
| `LOCAL_REASONING` | `auto` | Local thinking mode: `on`, `off` or `auto`. |
| `LOCAL_REASONING_EFFORT` | `low` | Local thinking effort: `default`, `low`, `medium` or `xhigh`. |

### Tests and diagnostics

Offline tests:

```sh
uv run pytest -q
```

Live scenarios use the configured model and incur its normal inference cost:

```sh
uv run python -m scenarios
uv run python -m scenarios --only 1,4,5
```

Results print as PASS/FAIL. Temporary evaluation state is cleaned up automatically.
Add `--keep-state` to retain results, or `--state-dir PATH` to use and preserve a specific directory.

Browse **Memory & artifacts** in the web sidebar, or search from the CLI:

```sh
uv run chatbot memory-search s1 "manager"
uv run chatbot capabilities
```

Development references: [adding a capability](docs/CAPABILITIES.md),
[design notes](NOTES.md), [evaluation coverage](docs/EVAL_REVIEW.md) and
[Slack evaluation](docs/SLACK_EVALUATION.md).
