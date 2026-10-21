# NOTES

> Working log now; condensed into the one-page deliverable at the end.

## Live results (scenarios 1, 4, 5 — 2026-10-20)
| Model | S1 cross-thread | S4 changed mind | S5 stays local | Calls | Max input | Pre-flight ≥ actual |
|---|---|---|---|---|---|---|
| Claude Sonnet 5.5 | PASS | PASS | PASS | 22 | 1,160 | 22/22 (server count) |
| Muse Spark 1.3 Contributor | PASS | PASS | PASS | 22 | 2,523 | 22/22 (server count) |

Scenario 2 (Claude, 500 ingested turns, 2026-10-20): PASS. "Which caterer ... deposit?" →
"Saffron Table ... $340, due by Thursday" (turn 20); partner "Sam" (turn 60). Ingest took
175 s and 72 calls (53 extract, 19 summarize); 75 calls total, all within budget, max chat
input 5,227 tokens.

Scenario 3 (Claude, 2026-10-20): PASS. Q3-plan is 35,370 tokens (Claude count). "Summarize doc
Q3-plan." took 95 s (36 map/reduce calls, note cached); after 100 ingested turns, "What was the
launch date in that plan?" → "Tuesday, September 29, 2026" plus the other milestones. 55 calls,
all within budget, max chat input 6,968.

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
- R11. 2026-10-20 design review: user chose tool-driven session/long-term memory and deferred
  detailed scope policy. Existing harness injection/extraction is not this architecture yet.
  Context shrinking takes priority over estimator tuning; current tools may load upfront.
- R12. Implementation inspection: extractor assigns all facts in a batch to its last turn;
  message and index writes commit separately; async requests are not serialized across the
  memory pipeline. Fact sources need exact message IDs (potentially multiple), and index
  updates need atomicity or a durable repair path. Forget orchestration is not implemented.
- R13. Current fit_history drops only older replayed turns; it cannot bound the current run
  once accumulated tool exchanges alone fill context. Core retries restart the run and can
  repeat completed tool work. Add checkpoints/result references and mutation idempotency.
- R1. Hybrid retrieval (FTS5 + local embeddings): measure after prototype whether embeddings
  are needed or FTS5 + query rewriting is enough.
- R2. ~~Python 3.13 wheels~~ — fastembed 0.9 and sqlite-vec 0.1.9 work on 3.13 / Windows.
- R3. Meta model id `muse-spark-1.3-contributor` is built into PydanticAI's `MetaProvider`
  (`https://api.meta.ai/v1`). Still to confirm with a key: usage fields and whether the
  token-count endpoint works. Local Bonsai 2: confirm server and model name.
- R4. Local estimate (chars/3 + 8 per part) undercounted Meta by up to 31% (structured-output
  framing is hidden from us). Now: server count for Claude and Meta (Meta's overcounts ~2x, safe),
  estimate x1.5 only for local servers. Re-measure on Bonsai 2.
- R5. Extraction adds one model call per live message (~1.0-1.2k input tokens). Options:
  skip obviously fact-free messages with a cheap heuristic, or run extraction in the background.
- R6. ~~Profile lists every fact~~ — done in M4: preferences in instructions, facts ranked by
  relevance within FACTS_TOKEN_BUDGET. The extractor still keeps some chatter ("X had coffee
  and might join project Y"); tighten further if it hurts ranking.
- R7. ~~Chat prompt caching misses~~ — fixed: explicit breakpoints after instructions and before
  the current message (`CachePoint`). Live, 5 consecutive Slack turns read 2,068 → 3,229 cached
  tokens per call (~60-75% of input); before, 0. The window had also been sliding every turn
  (see W10).
- R8. A single user message larger than the budget still fails after the retries; treat it
  like a big document (chunk + summarize) or reject it with a friendly reply.
- R10. First fetch of a big doc takes ~95 s (15 map calls). Could precompute notes when docs
  are generated/changed, or stream a quick partial answer.
- R9. Schema changes are not migrated: delete `.state/state.db` after pulling (pre-release).

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
- W8. Claude Code's first M4 design dumped all facts into the instructions. The 500-turn live
  run produced 70 facts (~5.8k tokens) and pre-flight refused the chat call at 11.9k. Caught by
  the budget guard on the live run; facts are now relevance-ranked within a budget.
- W9. Retrieval first injected 6 snippets into every message (even "latest") because vector
  nearest neighbours always exist. Caught by a unit test; vector hits now need distance <= 0.35.
- W10. Claude Code's token estimate counted every replayed request's stored `instructions`
  (~1.2k tokens each), though providers only get the latest. Live turns looked ~1.3k tokens, the
  history window shrank to 2 turns and its start moved every turn, defeating the cache. Caught by
  checking cache reads live (R7). Now only the latest instructions count, stored runs drop
  instructions, and replayed tool output is cut to a fixed stub.
- W11. The history generator used one random number for both dates and hours ("dentist at
  26:30"). Caught when the bot asked for a real time in the live run.
- W12. Claude Code's first doc reducer grouped notes by size only; reduce outputs (~3.8k chars)
  often didn't pair under the 7.5k group limit, so the note count stopped shrinking: 149 calls,
  892 s for one summary. Caught in the live scenario 3 log. Groups now take >= 2 clipped notes.
- W13. Context assembly sized only the first request of a turn; fetch_doc + search_doc results
  pushed the next request to 8,506 tokens (refused by pre-flight, then retried). Caught in the
  same log; a ProcessHistory hook now re-fits every request in the tool loop.
- W14. A Claude Code test captured the message list PydanticAI mutates in place, so both
  "requests" looked identical; and a later version was sized so nothing needed trimming (a
  vacuous pass). Caught by debugging the processor directly.

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
| 2026-10-20 | | M4: hybrid search, retrieval, summaries, bounded context; scenario 2 passes live |
| 2026-10-20 | | R7: cache breakpoints, instruction-count fix, stable window; cache reads verified live |
| 2026-10-21 | | M5: docs generator, passage search, map-reduce notes, doc tools; scenario 3 passes live |
