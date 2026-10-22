# Plan

Spec: [docs/PROBLEM.md](docs/PROBLEM.md) · Stack: [docs/DECISIONS.md](docs/DECISIONS.md) · Rules: [AGENTS.md](AGENTS.md)

## Approach
One core `send(user_id, thread_id, text)` on PydanticAI. Keep each model request under the
configured input budget, including requests within a long tool loop. Prioritise a context
shrinking mechanism that preserves ongoing work and can recover omitted evidence via tools;
token-estimation precision is a separate follow-up.

Target architecture: agent-controlled, bounded read/write/search tools for session memory and
long-term memory, backed by SQLite. Do not prefetch facts, preferences, summaries, or search hits
into every prompt. Keep a bounded recent conversation window; older conversation and document
segments remain accessible through tools. Session memory is persisted but isolated to the
session; persistence does not imply cross-session visibility. Detailed scope/lifetime policy is
open, subject to the assignment's sharing, local-instruction, preference-history and forgetting
scenarios. Start with current tool schemas and instructions loaded and measure their cost.

Implementation status (2026-10-22): sequence steps 1-7 are implemented. Storage contracts,
source + note search, ask_memory subagent and write tools (injection and automatic extraction
removed), long-run compaction and checkpoints, forget, scenario runner (Claude 7/7 twice; Meta
7/7 from the re-scored log), HTTP/web/Slack surfaces and README/NOTES. Open items: docs/WORKLOG.md
"To revisit"; the real Slack DM round trip and the screen recording are manual.

## Next design/refactor work
1. Define bounded session/long-term search, read and write contracts, stable record IDs,
   versioning and source references. Enforce store ownership in handlers.
2. Add agent memory tools and verify cross-session recall without harness-injected memory.
   Keep ingestion on the shared pipeline and define how it produces tool-written memory
   without generating a user-facing reply.
3. Serialize mutations initially; fix batch source attribution and recoverable index updates.
   Design forgetting against actual stored records, derived notes and serialized tool exchanges.
4. Make shrinking work during a single long run after recent history is exhausted: persist a
   bounded working-state checkpoint and externalise completed tool results behind record IDs.
   Preserve active tool call/return pairs and avoid replaying completed mutations on retries.
5. Expose document segments through session memory references to versioned source passages;
   retain exact source text for details omitted by summaries. Keep long-term promotion explicit.
6. Implement `ask_memory(question)` as the main agent's recall interface, backed by a bounded,
   read-only memory subagent searching source records and summary notes with FTS5/BM25.
   Compare the existing vector search as an additional candidate source; reranking is optional
   and measurement-driven. Every child call uses the same budget/logging guard.
7. Exercise tasks exceeding 100k aggregate tokens across many <=8k requests, checking recall,
   local isolation, preference changes, deletion and recovery after compaction/restart.

## Memory architecture and contracts (accepted 2026-10-21; implementation pending)

Concrete interfaces, ids, ownership and mutation rules: [docs/MEMORY_CONTRACTS.md](docs/MEMORY_CONTRACTS.md).

### Durable storage versus active context
- SQLite persists original messages, versioned document passages, derived summary notes,
  working-state checkpoints and long-term records. Indexes locate records; they do not replace
  persistence or restore details lost during summarisation.
- Original evidence remains available until explicitly forgotten. Notes reference exact source
  IDs/versions, preserve uncertainty and identify their covered messages or passages. Summaries
  are derived navigation aids, not independent corroboration of their sources.
- Session records survive restarts but are visible only within that session. Cross-session
  recall uses explicitly promoted long-term records, with access to their permitted evidence;
  promotion never grants access to unrelated local conversation. Detailed lifetime policy stays
  open. Current/historical preference versions are explicit, not inferred from search rank.
- Keep only bounded recent conversation and necessary continuation state in active context.
  Do not inject memory contents or a complete title catalog at session start or on every turn.

### Read path: a question, bounded investigation, supported answer
1. Main agent calls `ask_memory(question, store=session|long_term|both)` when recall is needed.
   User/session identity and permitted stores come from trusted run dependencies, never arbitrary
   agent-supplied owner IDs. Include a short referent hint when the question would be ambiguous.
2. A fresh read-only memory subagent searches both original evidence and summary notes using
   FTS5/BM25, with ownership/store filters applied before candidate selection. It can reformulate
   queries, read records by ID, expand adjacent messages and inspect preference versions.
3. Fuse/deduplicate bounded candidate lists while retaining representation and source identity.
   Check original evidence for exact claims, tentative versus confirmed statements and later
   corrections. Summary hits must not gate all source retrieval: search raw records as well so
   an omitted detail remains discoverable.
4. Return `status` (`found`, `not_found`, `conflicting`, `incomplete`), a concise `answer`, source
   IDs/versions with short supporting excerpts, and material caveats. A missing hit means not
   found in this search, not proof that the user never supplied the information. Main agent can
   request another search or read cited evidence; internal IDs need not appear in user replies.
5. Configure limits on search/read calls, candidates, excerpt sizes, total model requests and
   output size. Start with a <=1,500-token parent-facing result and a small configurable step
   budget; tune using evaluations. Every subagent request stays within MAX_INPUT_TOKENS and is
   tagged `purpose=memory_search`. No recursive subagents or full-catalog scans.
6. Keep direct low-level search callable for offline comparison and diagnostics. Compare BM25
   over raw records alone, BM25 over raw + notes, and raw + notes with existing vector candidates.
   BM25 + summaries is lexical retrieval, not a substitute for measuring paraphrase recall.
   Add a reranker only if better evidence selection justifies extra latency and complexity.

### Write path and maintenance
- Main agent uses explicit bounded session/long-term write tools; memory search remains read-only.
  Writes include record type, source references and intended store; handlers validate ownership,
  size, source validity and version relationships. Never let a search summary silently become a
  new independent fact. Serialize mutations initially and make retries idempotent.
- Persist raw live/ingested turns through the same core path. Agent writes capture durable facts,
  preferences and session task state; these are separate from automatic source persistence.
  Recall/write instructions should require checking preferences before drafting, searching before
  answering personal-history questions and recording explicit durable updates before returning.
- Create bounded source-linked episode notes incrementally from committed conversation segments;
  use the same maintenance path for live and ingested history. Notes are searchable, not injected.
  Initial implementation assumption: non-overlapping fixed-size segments plus on-demand task
  notes; measure segment size and note latency rather than building a recursive summary tree now.
  Retain incomplete/retryable maintenance state, and search raw recent records before notes exist.
- Store document source passages once per version and attach session-owned references and notes.
  Generate full-document summaries through bounded map/reduce when needed; targeted memory search
  must still be able to return an exact passage. Long-term promotion remains explicit.
- A persisted checkpoint captures goal, decisions, pending work and record IDs before compaction.
  Externalise completed tool results behind references, preserve active call/return pairs, and
  resume without repeating completed mutations. Apply the same bounded-loop discipline to search.
- Forget removes matching evidence, versions, derived notes/checkpoints, indexes, outbound logs,
  serialized exchanges and cached answers. Track source dependencies for invalidation/rebuilding;
  prevent in-flight work from restoring deleted data and verify on disk after maintenance.

### Implementation sequence and acceptance
1. Storage contracts: exact source links, versions, session ownership, serialized/idempotent
   mutations, atomic index updates or a durable repair path; preserve existing state with an
   explicit migration/reindex strategy rather than silently discarding it.
2. Searchable source + note indexes and bounded read/search handlers; deterministic retrieval
   tests cover raw-only details, paraphrases, adjacent context and misleading summaries.
3. Memory subagent and `ask_memory` capability, explicit write tools, agent recall/write policy;
   remove harness memory injection and replace automatic fact extraction deliberately. Ingest
   may skip the user-facing reply, but must exercise the shared memory-write/maintenance path.
4. Source-linked note maintenance, document references and checkpoint-based continuation; test
   restarts, maintenance failure recovery, long current runs and no duplicate mutations.
5. Preference history and complete forget orchestration, including all parent/child tool traces;
   verify source correction/deletion invalidates derived answers and no local data leaks.
6. Run scenarios 1-7 and variations on 500-turn histories and >100k aggregate-token workloads.
   Measure evidence recall separately from final answer correctness, unsupported/stale answers,
   tool-use omissions, scope leakage, latency, total calls and every call's reported input tokens.
   Historical scenario passes describe the old implementation, not acceptance of this refactor.
7. Finish real Slack/main-chat demo and deliverables. Retain upfront current capability loading;
   measure its cost before adding dynamic discovery. Estimator tuning remains a separate task.

## Original milestones (historical sequence; next work follows the refactor above)
1. **Skeleton:** uv project, config, model factory (Claude / Meta / local), budget hook + call log, `send()` echo via CLI.
2. **Storage:** SQLModel tables, message persistence, ingest path for seeded history.
3. **Facts + scopes:** extraction with `user` / `thread` / `ephemeral` scope and versioning → scenarios 1, 4, 5.
4. **Long thread:** rolling summaries + hybrid search → scenario 2.
5. **Docs:** `gen_docs.py`, chunked reading of large docs, doc notes → scenario 3.
6. **Capabilities:** registry, `send_message` log, LinkedIn post with validator → scenario 7.
7. **Forget:** hard delete everywhere + DB grep test → scenario 6.
8. **Scenario runner** printing PASS/FAIL; fix until green.
9. **Surfaces:** FastAPI `/send`, Slack Bolt (Socket Mode), assistant-ui web page.
10. **Write-up:** README, NOTES one-pager, screen recording.

Cut order if short on time: web UI → embeddings (keep keyword search only) → polish.
