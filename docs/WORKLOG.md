# Work log

> Full working log. The one-page deliverable is [../NOTES.md](../NOTES.md).

## Authorized channel mentions implementation (2026-10-22 Pacific)

User explicitly expanded the original DM-only scope: one core conversation per
Slack channel (`slack-` + channel ID), retaining endless `slack-dm`. Working tree
was clean before edits. Policy: channel replies under the mentioned message (or
existing Slack parent); every channel follow-up requires a mention. All channel
members allowed by SLACK_ALLOWED_USER map to the same single-user identity.

Fixed post-forget fulfilled-request replay: replace deleted assistant replies
with a neutral completion boundary when the user request survives; no forgotten
content is retained. Regression verifies history shape and disk deletion. New
regression also exposed lazy vector-table creation deadlocking inside forget's
write transaction on unindexed messages; deletion now visits existing vector
indexes on the same connection, including historical embedding dimensions.

Implemented durable reply-reference delivery receipts (no extra
personal text), per-conversation generation/delivery serialization, bounded
listener retries, explicit event filters and mention normalization, conservative
Slack mrkdwn rendering. Initial targeted run: 31 tests pass; full implementation run: 140 tests pass. Test authoring
mistake caught: record APIs return RecordView.ref and reject invisible sources
with ValueError, rather than returning None; corrected assertions.

Verified current Slack app A0CADLS4P0D via settings UI: Socket Mode enabled,
bot events app_home_opened, app_mention, message.channels, message.groups,
message.im. auth.test response confirms granted app_mentions:read, chat:write,
im:history, im:read among other scopes. No settings changes/reinstall required.
conversations.info denied missing_scope (channels:read absent); membership verified from visible joins and successful live replies in #test and
#dmagent. Full live evidence and remaining limits: SLACK_EVALUATION.md, channel
mentions section. Channel -> second channel/DM/main recall, draft isolation,
thread routing, rapid request ordering, common Slack formatting, cross-surface
forget and post-forget non-revival PASS. 70 model calls including final restart smoke check, max input 4,429, no
violations or refusals. Added retry-backoff ordering regression after inspection
found the first listener loop released the lock between attempts; retries now
hold their conversation's place. Synthetic fact deleted through core; no local
DB/WAL bytes remain; Slack messages retained as evidence.

Concurrent unrelated agent/shrink.py and agent/token_count.py appeared during
validation. Preserved them. Repository-wide ruff had passed before they appeared;
a later global run flags long lines in unrelated untracked shrink.py. Checks on
this feature's changed Python files pass. No unrelated files were staged.

Final reviewed snapshot 2014ed5: 141 tests PASS (25.72 s); Ruff checks and format
verification PASS across tracked Python directories (67 files). Repeated current
checkout tests were interrupted when concurrent budget work changed their code;
reviewed source was exported to an isolated directory for a reproducible run.
Runner mistake caught: the first archive run still imported the editable install;
fixed with explicit PYTHONPATH and verified module path. The bot now runs reviewed
source from .state/runtime/slack-2014ed5 with the original repo config/state root;
no secrets copied or settings changed. Final real #test mention returned exact
FINAL-MENTIONS-964 in its own thread in 2.7 s. Independent crash/exhausted retry,
progress feedback and post-forget latency limitations are explicitly documented.

## Cross-conversation Slack evaluation follow-up (2026-10-21 Pacific)

Extended [SLACK_EVALUATION.md](SLACK_EVALUATION.md) with existing-session recall,
main-chat -> Slack, Slack update -> two main sessions, version-chain inspection,
thread/top-level draft-only isolation and cross-surface forget. Shared persistence
and deletion passed; #test facts never entered the core (channels unsupported).
Found a new behavioral regression after forgetting: the Slack thread answered a
coordinator recall question but also revived an earlier one-off draft prefix.
Deletion left old user requests with their assistant replies removed; replay is
the suspected mechanism, not proven causal. Follow-up top-level draft was clean.
Provider changed externally mid-run: Slack stayed Claude; later CLI probes used
Bonsai. Report separates this limitation. 119 tests passed; 122 live calls,
max input 7,144, zero violations/refusals. No source changes.
Evaluation mistake caught: used `supersedes` instead of `supersedes_id` in a
read-only inspection probe; corrected via the actual model fields.

## Slack integration evaluation (2026-10-21 Pacific)

Evaluated the running build in real Hazel DMs and #test; report:
[SLACK_EVALUATION.md](SLACK_EVALUATION.md). Started the previously stopped Slack
process. DM response, cross-surface recall, Slack thread routing and local forget
passed. Found failed-event deduplication suppressing retries (offline repro),
out-of-order rapid replies and unconverted Markdown (live). Confirmed channel
mentions are unsupported even after inviting Hazel; this matches DM-only scope.
116 offline tests passed; 26 live model calls, max input 5,269, no violations.
Assumption: evaluation authorizes synthetic QA messages and a separate CLI recall
session; retained Slack messages as evidence, changed no production code.
Computer-use activation/bounds errors required using the signed-in Slack browser.
Evaluation mistakes caught: corrected a malformed inline Python probe; a plain
SQLite dump could not load vec0, so checked the DB bytes directly for the forget
probe. Neither failure was treated as a product defect.

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

Scenario 7 (Claude, on scenario 2's 500-turn history, 2026-10-21): PASS. Post: 906 chars,
3 hashtags on the last line, no links, facts from Q3-plan (launch September 29, 2026; beta
August 18). 44 calls (36 for the first Q3-plan note), all within budget; max 7,855 input tokens
(see R14). Live `send_message`: "Message my partner..." → recorded to Sam.

Meta needed fixes before passing (W5–W7). Muse answers "write one sentence about our favourite
organisation's logo" by saying it doesn't know the organisation, where Claude writes the
sentence with a placeholder; spelling is correct either way.

Tool-first memory (PLAN step 3, Claude, 2026-10-21): scenarios 1, 4, 5 PASS with no injected
memory. The agent saved facts/preferences itself, superseded British -> American via
`update_memory`, and explained the change from the version chain. 179 calls (32 chat, 147
memory_search), all within budget, max 6,426 input tokens. Historical passes above describe the
old injection design.

Forget (PLAN step 5, Claude, 500-turn history, 2026-10-21): scenario 6 PASS. Address (turn 100)
absent from replies, from send_message (none sent; the bot asked for the address instead) and
from the database files on disk. 54 calls, all within budget, max 6,352 input tokens. Recall gaps
seen: "Where do I live?" missed the "New address is..." message (vocabulary gap) and the bot didn't
look up the partner's name before asking; subagent prompt now rewords before not_found.

## Assumptions
- A5. 2026-10-21 memory plan: persisted source evidence + source-linked summary notes, searched
  together through BM25 behind a bounded read-only memory subagent. Evaluate existing vectors
  rather than mandate or remove them. Session persistence does not grant cross-session access.
  Initial note grouping uses bounded non-overlapping segments; exact size and detailed lifetime
  policy remain tunable. Full design/implementation sequence: PLAN.md; decisions D29-D30.
  This is accepted design, not implemented runtime behaviour or a new scenario PASS.
- A1. Single user, hard-coded `user_id` mapping between main chat and Slack.
- A2. Runtime state, including the generated document corpus, lives in gitignored `.state/`
  inside the repo dir (satisfies "SQLite or files inside the repo directory"; keeps user data out
  of git). `fetch_doc` reads `.state/docs/`; graders generate it with `scripts/gen_docs.py`.
- A3. "Styled" web UI is allowed even though the brief says no styling is needed.
- A5b. Ingested history goes through the same pipeline (storage, indexing, note maintenance)
  but runs no agent, so it produces no tool-written records; ingested facts are recalled from
  raw messages and notes via ask_memory.
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
- R12 progress (2026-10-21): the new memory store (records.py) has exact source refs,
  serialized + idempotent writes and transactional record indexing; messages get a startup
  reindex repair. Legacy extraction still attributes a batch to its last turn until it is
  replaced (PLAN sequence 3).
- R13 (addressed in step 4: in-run compaction + content idempotency). Current fit_history drops only older replayed turns; it cannot bound the current run
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
- R14. Estimate vs real: the history hook kept a LinkedIn turn's third request under the
  6,400-token estimate target, yet Claude counted 7,855 (~23% more: tool exchanges, thinking
  blocks, template text). The guard still holds; tighten via estimation work or by asking the
  provider count inside the hook (deferred per PLAN: estimation precision is a follow-up).
- R15. Replies sometimes cite internal turn numbers ("my earlier answer in turn 506") because
  snippets are labelled by turn; label by date instead.
- R16. Measured (scripts/eval_retrieval.py): 12/14 recall@5 for every configuration; notes and
  vectors added nothing on this set. Misses are paraphrases ("midday"->lunch, "morning
  sync"->standup). Default stays BM25; vectors optional (D36).
- R17. ask_memory is used on most turns (policy: check preferences before drafting, recall
  before personal answers): ~4.6 subagent requests per chat request live. Measure and tune in
  PLAN step 6 (fewer steps per question, skip preference checks for non-drafting turns, cache
  answers within a turn).
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
- W15. Claude Code's first LinkedIn fallback, after the writer exhausted its retries, assembled
  the post from the *brief* instead of the writer's last draft. Caught by a unit test; the
  fallback now trims the last draft deterministically.
- W16. Claude Code proposed "on-demand capability instructions" for M6, against the user's
  recorded D24 (load upfront, measure first). Caught by reading the repo before starting;
  confirmed with the user and built upfront loading with a cost report.
- W17. While mutation-testing the new version checks, Claude Code used `git checkout` to
  restore a file that was not committed yet; the restore failed silently and the suite ran
  against the mutated file (2 failures). Caught from the test output; restored from the
  /tmp backup. Lesson: commit (or copy) before mutating.
- W18. First live run of the tool-first memory: the subagent tried to open the source
  message of a long-term record from another session (correctly invisible) and reported its
  absence as a caveat; the main agent also showed internal refs ("rec:2") and said lookups
  "failed" when the subagent hit its step limit on empty searches. Caught by reading the live
  transcript; prompt fixes (cited excerpts are the evidence, not_found after 2-3 empty
  searches, never show refs).
- W19. Claude Code's scenario runner counted a request the guard refused at pre-flight (never
  sent, nothing billed) as a budget violation, failing Meta's scenario 7 although every sent
  call was under 8k. Caught by inspecting the flagged log line; budget is judged on sent calls,
  refusals reported separately.
- W20. Claude Code disabled prompt caching for the memory subagent ("every prompt is unique"),
  but each step of one question resends the previous steps: 604k uncached input tokens in a
  full run. Caught in the run's per-purpose cost breakdown; with caching, 80% are cache reads.
- W21. After the generic rewording rule, live S6 regressed: the search subagent found the
  address within 2-3 searches but kept verifying until its 6-request limit and returned no
  answer, so nothing was forgotten. Caught by the S1/S4/S6 re-run; traced by replaying the
  subagent's steps. Fixes: answer-early rule, tools stop at the last request, evidence kept on
  limit. Re-run: S1/S4/S6 PASS, subagent requests per chat request 4.2 -> 2.6.
- W22. A retried turn (pre-flight refusal) rebuilt the run deps and dropped what the first
  attempt's tools recorded (values forgotten), so the reply wasn't scrubbed. Surfaced by
  test_forget once the system prompt grew; outputs are now shared across retries.

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
| 2026-10-21 | | M6: capability registry, send_message, LinkedIn writer + validator; scenario 7 passes live |
| 2026-10-21 | | Memory refactor step 1: contracts doc + storage layer (records, sources, versions, ownership, repair, migration) |
| 2026-10-21 | | Memory refactor step 2: episode notes, fused source+note search, deterministic retrieval tests |
| 2026-10-21 | | Memory refactor step 3: ask_memory subagent, write tools, injection/extraction removed; S1/S4/S5 live |
| 2026-10-21 | | Memory refactor step 4: doc passages in search, in-run compaction + checkpoints, mutation keys, recovery tests |
| 2026-10-21 | | Step 5: forget sweep + tool + disk purge (S6 live PASS); step 6 started: scenario runner, retrieval eval |
| 2026-10-22 | | Prompt fixes (memory before "don't know"), subagent answer-early + last-step guard, retry outputs fix; S1/S4/S6 re-run PASS |
| 2026-10-22 | | Step 6: full runs - Claude 7/7 twice, Meta 7/7 (log re-scored); subagent caching; step 7: HTTP, Slack (connects), web UI, README, NOTES |
| 2026-10-22 | | Local Bonsai 2 27B via PrismML llama.cpp fork; autostart from the model factory + `chatbot local-server` (D37) |
