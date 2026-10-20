# Plan

Spec: [docs/PROBLEM.md](docs/PROBLEM.md) · Stack: [docs/DECISIONS.md](docs/DECISIONS.md) · Rules: [AGENTS.md](AGENTS.md)

## Approach
One core `send(user_id, thread_id, text)` on PydanticAI. Each turn builds a prompt under the
8k budget: stable prefix (system, capability descriptions, user profile) + retrieved memories +
recent turns. Memory = SQLite tables for messages, scoped and versioned facts, rolling summaries,
doc notes, and a keyword + embedding search index. Forget = hard delete across all of them.

## Milestones (each one is a commit or more)
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
