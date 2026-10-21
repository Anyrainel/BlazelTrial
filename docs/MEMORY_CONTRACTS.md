# Memory contracts (PLAN step 1)

Concrete contracts for the accepted memory architecture ([PLAN.md](../PLAN.md) "Memory
architecture and contracts"; D23, D29, D30). This fixes interfaces, ids, ownership and mutation
rules; the build order is PLAN's implementation sequence. Nothing here is runtime behaviour yet.

## 1. Interfaces at a glance
| Who | Interface | Role |
|---|---|---|
| Main agent | `ask_memory(question, store)` | the recall interface (read-only, bounded subagent) |
| Main agent | `remember(...)`, `update_memory(...)` | explicit, validated writes (session or long_term) |
| Memory subagent only | `search`, `read`, `expand`, `versions` | low-level, read-only evidence access |
| Diagnostics / evals | same low-level handlers, callable directly | offline recall comparisons |

No memory contents, title catalogs or search hits are injected into prompts by the harness.

## 2. Stores and ownership
- **Caller** = `MemoryCaller(user_id, session_id)`, built only from trusted run dependencies.
  No handler or tool accepts an owner, user or session id from model-supplied arguments.
- **session** store: this session's original messages, its notes, its session records, and
  passages of documents this session opened. Persisted across restarts; never visible to other
  sessions.
- **long_term** store: records the user explicitly promoted, visible from every session of the
  user. Promotion grants access to the record and the excerpts it cites — never to the rest of
  the source conversation.
- `store` argument: `session`, `long_term` or `both` (default `both` for `ask_memory`).
- Ownership/store filters are applied inside the query (SQL `WHERE`) **before** candidate
  ranking, not by filtering results afterwards.
- Anything outside the caller's visibility is reported as **not found**, never "forbidden", so
  ids leak no existence.

## 3. Record ids (stable, typed; internal, need not appear in user replies)
| Ref | Points at | Visible to |
|---|---|---|
| `msg:<id>` | an original message (live or ingested) | its session |
| `note:<id>` | a source-linked episode note over a fixed, non-overlapping segment of messages | the session of its messages |
| `rec:<id>` | one version of an agent-written memory record | session: its session; long_term: its user |
| `psg:<id>` | a document passage of one document version | sessions that opened the document |
| `ckpt:<id>` | a working-state checkpoint (PLAN step 4) | its session |
Ids are never reused. Malformed or unknown refs get the same "not found" reply.

## 4. Records, versions, sources
- `MemoryRecord`: `store`, `kind` (`fact` · `preference` · `task`), `key` (topic slug ≤ 64),
  `text` (≤ 500 chars), owner fields, `created_at`, version links.
- **Explicit versions.** `update_memory(supersedes=rec:<id>, ...)` must name the current
  version (same store and owner). The new row links `supersedes_id`; the old one gets
  `superseded_at` + `superseded_by`. Rows are never edited in place, and superseding a
  non-current version is rejected (no forks). There is no implicit supersede by key.
  Current vs historical preference versions come from this chain, not from search rank.
- **Required sources.** Every write cites 1–10 refs visible to the caller, at least one of them
  original evidence (`msg:` or `psg:`). A `note:` or `rec:` may be cited only alongside the
  evidence it rests on, so a summary or search result never becomes an independent fact.
  Sources live in `RecordSource(record_id, ref)`, which also drives forget invalidation.
- **Notes** (`EpisodeNote`): fixed-size, non-overlapping message segments (size tunable),
  built by the same maintenance path for live and ingested history. They store covered
  `msg:` ids and preserve uncertainty. They are searchable navigation aids, never injected
  and never proof on their own.

## 5. Read path: `ask_memory`
- **Input**: `question` (with a short referent hint when ambiguous) and `store`.
- **Subagent**: fresh, read-only, no recursion. It may reformulate queries, `search`, `read`,
  `expand` adjacent messages and inspect `versions`, within configured step limits. Every
  request goes through the shared budget guard, tagged `purpose=memory_search`.
- **Search**: FTS5/BM25 over raw records **and** notes, each with its own candidate list, fused
  and deduplicated while keeping source identity. Raw records are always searched, so details
  a note omitted stay findable. Vector candidates are an optional extra source, kept only if
  measured recall gains justify them. Reranking is out of scope until measured.
- **Output** (≤ 1,500 tokens):
  - `status`: `found` · `not_found` · `conflicting` · `incomplete`
  - `answer`: concise; prefers original evidence over notes, and flags tentative statements
    and later corrections
  - `sources`: refs with short excerpts (≤ 300 chars each)
  - `caveats`
  `not_found` means "not found in this search", not "never said".

### Low-level handlers (subagent tools; bounded)
| Handler | Returns | Bounds |
|---|---|---|
| `search(caller, query, store, k)` | hits: ref, type, store, excerpt, current/superseded, created_at | k ≤ 10; excerpt ≤ 300 chars |
| `read(caller, ref)` | text (≤ 2,000 chars), metadata, version links, sources / covered messages | one item |
| `expand(caller, msg_ref, before, after)` | adjacent messages of the same session | ≤ 5 each side |
| `versions(caller, rec_ref)` | the record's chain, oldest → newest | ≤ 20 |

## 6. Write path: `remember` / `update_memory`
- **Arguments**: `store`, `kind`, `key`, `text`, `sources`, plus `supersedes` for updates.
  Handlers validate ownership, size, source visibility and the version relation.
- **Serialized**: one process-wide lock around every mutation, plus a SQLite transaction.
- **Idempotent**: the tool-call id is the idempotency key (unique). A retried write returns the
  original record instead of writing twice.
- **Atomic indexing**: a record's FTS row is written in the same transaction as the record.
  Message indexes get a durable repair path (`reindex_missing()` at startup) until they are
  written transactionally too.
- **Policy, enforced by the main agent's instructions**: check preferences before drafting,
  `ask_memory` before answering personal-history questions, and record explicit durable updates
  before replying. Draft-local instructions are not written to any store.

## 7. Forget dependencies (implemented in PLAN step 5)
Forget removes matching evidence (`msg:`, `psg:` refs held by the session) and every derived
item. `RecordSource` and the note→message links say what to invalidate: records, versions,
notes, checkpoints, index rows, outbound messages, serialized tool exchanges and cached
`ask_memory` answers. In-flight work must not restore deleted data, and deletion is verified on
disk after maintenance.

## 8. Migration (no silent loss)
An explicit, idempotent `migrate-memory` step converts existing `Fact` rows:
- user-scope facts → long_term records; thread-scope facts → session records;
- supersede chains are kept;
- each record is linked to the user message(s) of its source turn.
It is recorded in a migration table. Old tables remain until the harness injection and
automatic extraction are removed (PLAN sequence 3).

## 9. Open (tunable, measured)
Note segment size; subagent step budget; vector candidates on/off; long-term lifetime policy.
