# Memory contracts (PLAN step 1)

Concrete contracts for the accepted memory architecture ([PLAN.md](../PLAN.md) "Memory
architecture and contracts"; D23, D29, D30). This fixes interfaces, ids, ownership and mutation
rules; the build order is PLAN's implementation sequence. Nothing here is runtime behaviour yet.

## 1. Interfaces at a glance
| Who | Interface | Role |
|---|---|---|
| Main agent | `ask_memory(question, store)` | the recall interface (read-only, bounded subagent) |
| Main agent | `update_memory(change, scope)` | remember or change something; the worker finds what it replaces (session or long_term) |
| Main agent | `forget(what)` | erase what the user described, found by the worker |
| Memory worker only | `search`, `grep`, `list_records`, `read`, `expand`, `versions` | low-level, read-only evidence access |
| Memory worker, write mode | `save`, `revise` | validated record writes (the same handlers as before) |
| Memory worker, forget mode | `erase` | model-chosen strings/messages handed to the deterministic hard delete |
| Diagnostics / evals | same low-level handlers, callable directly | offline recall comparisons |

No memory contents, title catalogs or search hits are injected into prompts by the harness,
with one bounded exception: current **preference** records (long-term + this session's) form a
"Standing preferences" block in the chat instructions (always on, so it cannot depend on the model
remembering to look). Total size is capped per store (`PREFERENCES_MAX_CHARS` 1200,
`SESSION_PREFERENCES_MAX_CHARS` 400) and enforced in `records.write`: a preference write that
would exceed the cap is refused with the used/limit sizes. `retire(ref)` stops using a current
record without a successor (history stays); a retired record cannot be revised.

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
| `ckpt:<id>` | a working-state checkpoint of a compacted run | its session |
| `out:<id>` | a completed tool result moved out of a long run's context | its session |
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
| `search(caller, query, store, k, sources, newest_first)` | hits: ref, type (record/message/note), store, excerpt, current, created_at, `found_by`, turn and role (messages), covers (notes) | k ≤ 10; `newest_first` orders the chosen hits by date; excerpt ≤ 300 chars; ≤ 2,400 excerpt chars in total; one message hit per turn |
| `read(caller, ref)` | text (≤ 2,000 chars), metadata, version links, sources; notes: exact covered `msg:` refs | one item |
| `expand(caller, msg_ref, before, after)` | adjacent messages of the same session | ≤ 5 each side |
| `versions(caller, rec_ref)` | the record's chain, oldest → newest | ≤ 20 |

`sources` selects candidate lists for diagnostics and evals: `records`, `messages`, `notes`
(default) and optional `vectors` (message embeddings within the distance cut-off). Each list is
ranked separately (BM25 per FTS table) and fused by reciprocal rank; `found_by` names the lists
that returned a hit. CLI: `chatbot memory-search SESSION "query" --sources messages,notes`.

### Note writer (maintenance only, never a model tool)
`add_note(user_id, session_id, message_ids, text)`: one contiguous run of the session's own
messages, not overlapping any existing note, text ≤ 1,500 chars; note row, `NoteSource` rows and
FTS row commit in one transaction under the write lock.

### Long current runs (PLAN step 4)
Before every model request the history hook drops old replayed turns; if the current run alone
is still over target, its completed tool results (all but the latest) are saved as `out:` refs
(idempotent per tool call) and replaced in context by a stub naming the ref, and the run's goal
and completed calls go to a `ckpt:` checkpoint. Call/return pairs stay intact. The main agent
reads any of these back with `read_memory(ref)` (bounded; session-private).

## 5b. One worker, three modes (D42)
`ask_memory`, `update_memory` and `forget` each run the same bounded worker (fresh per task, no
recursion, trusted caller, shared budget guard; `purpose=memory_search|memory_write|memory_forget`)
with `worker.md` (shared principles) plus one prompt per mode. Tools by mode: read =
`search grep list_records read expand versions`; write = read + `save revise`; forget = read +
`erase`. `list_records` surveys up to 50 current records (text shortened); `save` finds a same-key record by query, not by listing. Every worker task states today's date, and a write task ends with the user's own message text. `grep` is an exact case/whitespace-insensitive scan (numbers, codes, names that BM25
ranks badly); scoped to the caller's visibility, except in forget mode where it covers every
conversation, saved outputs and sent messages. A step limit returns `incomplete` with what was
already done.

## 6. Write path: `update_memory` (worker `save` / `revise`)
- **Arguments** (worker tools): `save(store, kind, key, text, sources)` and
  `revise(record, text, sources)`; the user's current message is always cited. Handlers validate
  ownership, size, source visibility and the version relation; `save` refuses to duplicate a
  current record under the same key (it names the record to revise instead).
- **Serialized**: one process-wide lock around every mutation, plus a SQLite transaction.
- **Idempotent**: the key is the turn (current message) + tool + arguments, so a repeated or
  retried write in the same turn (even from a restarted run with new tool-call ids) returns the
  original record; `send_message` uses the same rule.
- **Atomic indexing**: a record's FTS row is written in the same transaction as the record.
  Message indexes get a durable repair path (`reindex_missing()` at startup) until they are
  written transactionally too.
- **Policy, enforced by the main agent's instructions**: check preferences before drafting,
  `ask_memory` before answering personal-history questions, and record explicit durable updates
  before replying. Draft-local instructions are not written to any store.

## 7. Forget (implemented in PLAN step 5)
User revision (2026-10-22): `MEMORY_FORGET_MODE=soft` is the default. Chat history is not
memory and is never removed: a message containing a forgotten value (or forgotten whole by ref,
with the rest of its turn) gets `Message.forgotten = true`, keeps its text, and is excluded
from every memory path: index rows deleted and never re-created, `read`/`expand`/`grep`/search
treat it as not found, replay uses a neutral placeholder pair, note prompts show `[forgotten]`,
its stored run is cleared. The HTTP history returns it as written with `forgotten: true`.
Everything derived is truly deleted, as below (records and their excerpts, notes, saved outputs,
checkpoints, sent messages, legacy rows). Consequently the original bytes still exist in the
`message` table; scenario 6 checks the memory stores, not the chat log.
`MEMORY_FORGET_MODE=hard` retains the original disk-purge contract below, including purging
the messages themselves (including previously flagged ones).

`forget(what)` tool → forget-mode worker → `erase(values, messages)` →
`memory/forget.py:forget_values(user_id, values, message_ids)`: values ≥ 5 chars, matched case-
and whitespace-insensitively, user-wide, under the write lock, one transaction. The worker finds
how the information was written (`search` for meaning, `grep` for strings, `read` for exact
wording), erases each distinctive string, erases messages that discuss it without a distinctive
string by ref (the whole turn goes), then searches again with other words. `erase` refuses a
string matching more than `MEMORY_ERASE_MAX_MESSAGES` messages unless the worker confirms the
user meant everything, and reports any string still found afterwards. After the turn is stored, the core sweeps again (the turn's own run holds
the values in tool args and memory answers), redacts the reply, then optimizes FTS, truncates
the WAL and VACUUMs; the scenario check greps the database files on disk.

Dependencies:
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
Each migrated record keeps `legacy_fact_id` (unique), so reruns are incremental and
idempotent and link version chains that span runs (CLI: `chatbot migrate-memory`). Facts with
no surviving source message are migrated without sources and counted, not dropped. Old tables
remain until the harness injection and automatic extraction are removed (PLAN sequence 3).

## 9. Open (tunable, measured)
Note segment size; subagent step budget; vector candidates on/off; long-term lifetime policy.
