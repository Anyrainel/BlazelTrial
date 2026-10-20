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

## Where the AI was wrong
- W1. Claude Code claimed PydanticAI would "work against" a per-call token budget. The user
  pushed back; checking current docs showed `per_request_input_tokens_limit` +
  `count_tokens_before_request` and `ProcessHistory` hooks cover it. Switched to PydanticAI.

## Rejected AI suggestions
- (none yet)

## Time log
| Date | Hours | What |
|---|---|---|
| 2026-10-19 | | Spec, AGENTS.md, stack decisions |
| 2026-10-19 | | M1: uv project, config, model factory, budget guard + call log, CLI |
