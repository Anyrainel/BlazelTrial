# AGENTS.md

Shared instructions for **Claude Code and Codex**. Keep this file short; details live in the
linked docs. Spec: [docs/PROBLEM.md](docs/PROBLEM.md) (source of truth: `assignment.md`).
Stack rationale: [docs/DECISIONS.md](docs/DECISIONS.md).

## What this is
A single-user chatbot with one core, `send(user_id, thread_id, text) -> reply`, served to a
CLI / HTTP / web main chat (many parallel sessions) and Slack DMs (`thread_id="slack-dm"`, one
endless thread) plus explicit channel @mentions (`thread_id="slack-" + channel ID`,
one conversation per channel; authorized extension to the original assignment). The hard part is **memory + context under an 8k input-token budget**, not the UI.

## Stack
Python 3.13 · uv · PydanticAI (models: local Bonsai 2 27B default, autostarted; Claude Sonnet
5.5; Meta Muse Spark 1.3 Contributor) · SQLModel on SQLite (+ FTS5, sqlite-vec, fastembed) ·
FastAPI · Slack Bolt (Socket Mode) · pytest · ruff ·
web: React + Vite + TypeScript + assistant-ui (npm).

## Layout
```
src/chatbot/
  core.py            # send(): the ONLY entry point for every surface
  config.py          # pydantic-settings; all paths/limits from .env
  agent/             # PydanticAI agent, model factory, budget guard + call log, history fit hook
  memory/            # tables, records.py (memory store contracts), search, legacy extraction
  capabilities/      # registry.py + one subpackage per capability (docs, messaging, linkedin)
  docs_reader/       # chunked reading of large docs, persisted doc notes
  surfaces/          # cli.py, http.py (FastAPI, serves web/dist), slack_app.py
  prompts/           # prompt text files
web/                 # assistant-ui frontend
scenarios/           # scenario runner (PASS/FAIL) + fixtures/ (slack_history.py, docs.py)
scripts/             # gen_docs.py etc.
tests/               # offline unit tests (PydanticAI TestModel/FunctionModel)
.state/              # runtime only, gitignored: state.db, docs/, logs/, models/
```

## Commands
```bash
uv sync                                    # install Python deps
uv run python scripts/gen_docs.py          # generate .state/docs corpus (incl. Q3-plan)
uv run chatbot chat --thread s1            # interactive CLI session
uv run chatbot send USER THREAD "text"     # one-shot call to the core
uv run chatbot ingest FILE.jsonl           # load {"user","assistant"} lines into slack-dm via core
uv run chatbot capabilities                # capabilities + their per-request token cost
uv run chatbot migrate-memory              # copy legacy facts into versioned memory records
uv run chatbot reindex                     # index messages/records/notes missing from search
uv run chatbot memory-search S "q"         # diagnostics: fused memory search as session S
uv run chatbot local-server start          # llama.cpp Bonsai server (autostarts when LLM_PROVIDER=local)
uv run chatbot serve                       # FastAPI: POST /send, web UI at /
uv run chatbot slack                       # Slack app (Socket Mode)
uv run python -m scenarios                 # scenario script (real LLM), prints PASS/FAIL
uv run python scripts/eval_retrieval.py    # recall@5 of memory search configs (no LLM)
uv run pytest -q                           # unit tests, offline
uv run ruff check . && uv run ruff format .
cd web && npm install && npm run dev       # frontend dev (proxies to :8000); npm run build → web/dist
```
Config: copy `.env.example` → `.env`. `LLM_PROVIDER` ∈ `anthropic | meta | local`, plus
`LLM_MODEL`, `ANTHROPIC_API_KEY`, `META_API_KEY`, `LOCAL_BASE_URL`, `MAX_INPUT_TOKENS=8000`,
`STATE_DIR=.state`, `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`. Never hard-code a model id in code.

## Non-negotiable invariants
1. **One code path.** Surfaces are thin adapters over `core.send()`. Seeding history
   (scenario 2) also goes through `send()`; an ingest flag may skip the LLM reply, nothing else.
2. **Budget is enforced per request.** Every model is wrapped in `agent/budget.py:BudgetedModel`
   (built only by `agent/models.py:build_model`): pre-flight size check (provider count endpoint
   or conservative estimate) refuses oversized requests before sending; the provider-reported
   `input_tokens` (PydanticAI includes cache reads/writes) is logged as one JSON line in
   `.state/logs/llm_calls.jsonl` and must be <= `MAX_INPUT_TOKENS`. Tag calls with
   `call_context(thread_id=..., purpose=...)`. Never construct a provider model anywhere else.
3. **Prompt order for cache hits:** stable first (system, tool defs, user profile), volatile last
   (retrieved memories, recent turns). Compact only when `prompt_cache_outlook()` is cold or the
   budget forces it; compact in chunks so the prefix stays stable between compactions.
4. **History integrity.** History processors never split a tool call from its return.
5. **Memory design is evolving.** Agents read and write memory through bounded tools;
   do not automatically inject stored facts, preferences, summaries, or search hits every turn.
   Target recall interface: `ask_memory` backed by a bounded read-only subagent searching source
   records and source-linked summary notes with BM25 (PLAN.md; D29-D30). Session and long-term
   stores persist separately in visibility; detailed scope/lifetime policies remain open.
   Preserve acceptance behaviour: shared facts are recallable across sessions,
   and draft-local instructions do not leak into other sessions.
6. **Supersede, don't overwrite.** Changed preferences get a new version; the old one is marked
   superseded with a timestamp, so "what did I say before?" is answerable.
7. **Forget is a hard delete everywhere:** messages, facts, summaries, FTS + vector rows,
   doc notes, stored tool-call args, serialized PydanticAI histories. Test greps the DB file
   (after `VACUUM`) for the forgotten value.
8. **Large inputs are never inlined.** Tools return bounded output; docs > budget are read in
   chunks and distilled into a persisted doc note (keep key facts like dates verbatim).
9. **Capabilities are plugins:** name, one-line description, schema, `instructions.md`, handler.
   Start with all current tool schemas and instructions loaded; measure their token cost.
   Introduce tool search / dynamic discovery if growth warrants it. Hard output rules
   (LinkedIn: ≤1300 chars, ≤3 hashtags all at end, no links) are **validated in code** and
   repaired/retried, never trusted to the model.
10. **Generalise.** Evaluators run variations. Never special-case scenario wording, names,
    doc ids or turn numbers in `src/`.

## Conventions
- Type hints everywhere; small functions. SQLModel for tables; raw SQL only in
  `memory/search.py` (FTS5/vec virtual tables).
- Prompts are files in `src/chatbot/prompts/`, not inline strings.
- Unit tests never hit the network (use PydanticAI `TestModel`/`FunctionModel`). Scenarios use real LLMs.
- Frontend talks only to the HTTP API; no business logic in `web/`.

## Workflow rules
- Read `docs/PROBLEM.md`, `docs/DECISIONS.md` and `docs/WORKLOG.md` before non-trivial work.
- **Commit early and often**, small focused commits. Never rewrite history.
- Unclear requirement → reasonable assumption → append to `docs/WORKLOG.md` → keep going.
- New/changed stack decision → add a row to `docs/DECISIONS.md`.
- Log AI mistakes you catch and suggestions you reject in `docs/WORKLOG.md`; `NOTES.md` is
  the one-page deliverable summary.
- Never commit secrets, `.env`, `.state/`, `web/node_modules`, or AI transcripts.
- Done = `pytest` green + relevant scenario PASS + no budget violation in the call log.
