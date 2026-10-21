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

Implementation status: the existing harness still assembles memory blocks and runs automatic
extraction/summarisation. The tool-first architecture below is the next refactor, not completed.

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
6. Evaluate direct search first; a bounded read-only search subagent is an optional experiment.
   Every child call uses the same budget/logging guard. Measure recall, latency and total calls.
7. Exercise tasks exceeding 100k aggregate tokens across many <=8k requests, checking recall,
   local isolation, preference changes, deletion and recovery after compaction/restart.

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
