# NOTES

> Working log now; condensed into the one-page deliverable at the end.

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
- R4. Calibrate the local estimate (chars/3 + 8 per part) against provider-reported counts once
  keys are in; it must stay above the real count. (Claude uses the server count, exact so far.)
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

## Rejected AI suggestions
- (none yet)

## Time log
| Date | Hours | What |
|---|---|---|
| 2026-10-19 | | Spec, AGENTS.md, stack decisions |
| 2026-10-19 | | M1: uv project, config, model factory, budget guard + call log, CLI |
| 2026-10-19 | | M2: SQLModel tables, turn persistence, history window, ingest path |
| 2026-10-20 | | M3: scoped/versioned facts, extractor, profile; scenarios 1, 4, 5 pass live |
