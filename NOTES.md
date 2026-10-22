# NOTES

Full working log (assumptions, every AI mistake caught, measurements, time log):
[docs/WORKLOG.md](docs/WORKLOG.md). Decisions: [docs/DECISIONS.md](docs/DECISIONS.md).

## Design
- **One core**, `send(user_id, thread_id, text)`, behind CLI, HTTP/web (assistant-ui) and Slack
  (Socket Mode). Ingested history uses the same pipeline; only the model reply is skipped.
- **Budget guard on every model**: pre-flight count (provider endpoint, or estimate ×1.5)
  refuses oversized requests; provider-reported input (incl. cache) is logged per call and
  must be ≤ `MAX_INPUT_TOKENS`.
- **Context = static instructions + a bounded recent-turn window.** Nothing about the user is
  injected. A history hook re-fits every request of a turn: it drops old turns first, then
  moves the current run's completed tool results behind `out:` refs with a checkpoint.
- **Tool-first memory** in SQLite:
  - `ask_memory` runs a bounded read-only subagent. It searches BM25 indexes of versioned
    memory records, raw messages, source-linked episode notes and opened-document passages,
    and returns status + answer + sources.
  - `remember` / `update_memory` write explicit, source-cited, versioned records: session- or
    long-term-scoped, idempotent per turn.
  - `forget` deterministically deletes a value everywhere, then VACUUMs.
- **Big docs** are never inlined: a map-reduce note is cached per content hash, and passages
  are searchable.
- **LinkedIn rules** are enforced in code: a validator plus repair, and the post is returned
  verbatim.

## Trade-offs
- Tool-first recall costs calls: about 4 subagent requests per chat request. Prompt caching
  cuts subagent input by 80% in reads, but latency is higher than injection would be.
- Lexical BM25 by default: notes and vectors added no recall on a 14-question paraphrase eval
  (12/14 each). Paraphrase gaps rely on the subagent rewording queries.
- Forget matches literal values the agent supplies (after an `ask_memory` lookup).
  Deterministic and verifiable, but paraphrased mentions ("the Larkspur place") can survive.
- Ingest runs no agent, so ingested facts are recalled from raw messages and notes, not from
  records.

## Results
`python -m scenarios` (500-turn Slack history, real models):
- **Claude Sonnet 5.5: 7/7, twice.** 314 calls; max 7,440 input tokens; 0 over.
- **Muse Spark 1.3 Contributor: 7/7.** Scored from the run's call log after fixing the
  runner's accounting, not a fresh run. 167 sent calls; max 6,937 input tokens; 0 over; 1
  request refused at pre-flight and retried smaller.

107 offline tests; the key guarantees are mutation-checked.

## What doesn't work / not done
- A single user message larger than the budget fails after the retries (no chunking of
  pasted text).
- First `fetch_doc` of a big document takes ~95 s (map-reduce, 15+ calls).
- The bot sometimes doesn't look memory up before saying it lacks something (e.g. the
  partner's name after a forget).
- The local estimate undercounted Claude by up to ~23% on tool-heavy turns. The guard holds,
  but the estimator isn't tuned.
- No schema migrations: delete `.state/` after pulling. Slack: one DM thread, no
  files/threads/edits.

Time spent: ~__ hours (fill in).

## Where the AI was wrong (and how it was caught)
1. **Token estimate counted every replayed turn's stored instructions.** Providers only
   receive the latest ones. Each live turn looked ~1.2k tokens bigger, the history window
   shrank to 2 turns and slid every turn, and the prompt cache never hit. Caught by checking
   cache reads in the live call log after adding a cache breakpoint showed no effect.
2. **Doc map-reduce never converged.** Reduce outputs were too big to pair, so the note count
   stopped shrinking: 149 calls and 892 s for one summary. Caught in the live scenario 3 call
   log. Groups now always take two or more clipped notes.

## AI suggestion rejected
Claude Code's first memory design injected all remembered facts, preferences, summaries and
search hits into every prompt, filled by automatic extraction. I rejected it in favour of
agent-called memory tools: with hundreds of facts the injected block blew the 8k budget (11.9k
pre-flight) and was mostly irrelevant. It also made scope and versioning implicit. The
tool-first design keeps context small and puts recall behind an inspectable, source-cited
interface.
