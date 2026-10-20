# NOTES

> Working log now; condensed into the one-page deliverable at the end.

## Live results (scenarios 1, 4, 5 — 2026-10-20)
| Model | S1 cross-thread | S4 changed mind | S5 stays local | Calls | Max input | Pre-flight ≥ actual |
|---|---|---|---|---|---|---|
| Claude Sonnet 5.5 | PASS | PASS | PASS | 22 | 1,160 | 22/22 (server count) |
| Muse Spark 1.3 Contributor | PASS | PASS | PASS | 22 | 2,523 | 22/22 (server count) |

Meta needed fixes before passing (W5–W7). Muse answers "write one sentence about our favourite
organisation's logo" by saying it doesn't know the organisation, where Claude writes the
sentence with a placeholder; spelling is correct either way.

## Assumptions
- A1. Single user, hard-coded `user_id` mapping between main chat and Slack.
- A2. Runtime state, including the generated document corpus, lives in gitignored `.state/`
  inside the repo dir (satisfies "SQLite or files inside the repo directory"; keeps user data out
  of git). `fetch_doc` reads `.state/docs/`; graders generate it with `scripts/gen_docs.py`.
- A3. "Styled" web UI is allowed even though the brief says no styling is needed.
- A4. Budget number = provider-reported input tokens including cache reads/writes
  (PydanticAI's normalized `RequestUsage.input_tokens`).

## To revisit
- R1. Hybrid retrieval (FTS5 + local embeddings): measure after prototype whether embeddings
  are needed or FTS5 + query rewriting is enough.
- R2. Python 3.13 wheel availability (onnxruntime, sqlite-vec) on Windows.
- R3. Meta model id `muse-spark-1.3-contributor` is built into PydanticAI's `MetaProvider`
  (`https://api.meta.ai/v1`). Still to confirm with a key: usage fields and whether the
  token-count endpoint works. Local Bonsai 2: confirm server and model name.
- R4. Local estimate (chars/3 + 8 per part) undercounted Meta by up to 31% (structured-output
  framing is hidden from us). Now: server count for Claude and Meta (Meta's overcounts ~2x, safe),
  estimate x1.5 only for local servers. Re-measure on Bonsai 2.
- R5. Extraction adds one model call per live message (~1.0-1.2k input tokens). Options:
  skip obviously fact-free messages with a cheap heuristic, or run extraction in the background.
- R6. The profile block lists every active fact; with hundreds of facts it needs relevance
  filtering (search, M4) and a token cap.

## Where the AI was wrong
- W1. Claude Code claimed PydanticAI would "work against" a per-call token budget. The user
  pushed back; checking current docs showed `per_request_input_tokens_limit` +
  `count_tokens_before_request` and `ProcessHistory` hooks cover it. Switched to PydanticAI.

- W2. Claude Code's first token estimator measured the JSON of stored PydanticAI messages
  (timestamps, ids, metadata), inflating sizes several-fold, so a long thread replayed only 1
  turn. Caught by the history-window unit test; switched to counting only the content sent.
- W3. Claude Code's first live check for scenario 4 looked for "colour" vs "color", but the
  model wrote "favourite organisation" then "favorite organization" (correct behaviour). The
  check was wrong, not the bot; caught by reading the transcript. Scenario checks must test
  several spelling pairs.
- W4. A test fixture installed the fake chat model before another fixture cleared the agent
  cache, so tests silently hit the (blocked) real model. Caught by the failing tests; fixed by
  making the dependency explicit.
- W5. Claude Code's system prompt called memory "authoritative, prefer it on conflict". Claude
  coped, but Muse refused "Switch to American spelling" because memory still said British (it
  updates after the reply). Caught in the Meta live run; the newest message now always wins.
- W6. Claude Code's sync `send()` used `asyncio.run()` per call. Cached provider clients keep
  connections bound to the first (closed) loop, so the 2nd Meta call crashed with "Event loop is
  closed". Fixed with one shared `asyncio.Runner`; the first fix then broke 10 tests because
  the Runner reused the context vars captured on its first call (overrides invisible) — fixed by
  passing `context=copy_context()` per call.
- W7. Muse emitted a progress preamble (phase="commentary") that PydanticAI includes in
  `result.output`, and invented a user name ("Hi Maya"). Reply now skips commentary parts;
  prompt forbids invented personal details.

## Rejected AI suggestions
- (none yet)

## Time log
| Date | Hours | What |
|---|---|---|
| 2026-10-19 | | Spec, AGENTS.md, stack decisions |
| 2026-10-19 | | M1: uv project, config, model factory, budget guard + call log, CLI |
| 2026-10-19 | | M2: SQLModel tables, turn persistence, history window, ingest path |
| 2026-10-20 | | M3: scoped/versioned facts, extractor, profile; scenarios 1, 4, 5 pass live |
| 2026-10-20 | | Meta live check: loop fix, token-count fix, prompt + commentary fixes |
