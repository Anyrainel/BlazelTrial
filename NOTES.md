# NOTES

Implementation decisions and detailed validation: [docs/DECISIONS.md](docs/DECISIONS.md)
and [docs/WORKLOG.md](docs/WORKLOG.md).

## Design

- **One core:** `send(user_id, thread_id, text)` serves CLI, HTTP/web and Slack
  (Socket Mode, DMs and explicit channel mentions). History ingestion uses the same pipeline.
- **Token budgeting:** every live model request requires a successful provider token-count
  endpoint call (Claude/Meta), or the local server's template/tokenizer endpoints. Oversized
  requests are shrunk and counted again before inference; unavailable counting stops the
  call. Provider-reported input usage, including cache reads/writes, is logged against the
  configurable 8,000-token limit. Estimates help plan shrinking, but do not authorize live calls.
- **Context:** stable instructions and tools precede a bounded rolling window of recent
  history. The window moves in chunks to preserve prompt-cache reuse. Compaction keeps tool
  calls paired with their returns; large completed results can be recovered through references.
- **Memory:** SQLite holds LLM-managed, source-linked notes and versioned records categorized
  as facts, preferences or tasks. Long-term records are shared across conversations; session
  records stay local. Current preferences from both applicable scopes are always injected as
  a size-capped standing-preferences block. Other memory is reached through tools.
  `ask_memory` delegates recall to a bounded subagent with its own prompt and context. It
  searches records, notes, document passages and a BM25 session-history index, with exact
  text lookup and source reads for details that summaries may lose. Updates supersede records
  so earlier preferences remain explainable.
- **Forgetting:** the worker finds relevant sources, then deterministic code deletes derived
  records, notes, saved outputs and index entries. Default soft forgetting flags original chat
  messages: they remain visible to the user but are excluded from agent search, reads, replay
  and note generation. Hard deletion is also available.
- **Capabilities:** large documents are read in chunks and distilled into cached notes;
  LinkedIn length, hashtag and link rules are validated in code.

## Trade-offs

- A recall subagent keeps search iterations out of the main agent's 8K context, but its extra
  model calls make memory-heavy replies slower.
- I chose memory tools plus an indexed session history for more reliable recall of exact
  details from long conversations. This requires careful forgetting across every retrieval
  path while preserving the conversation the user can see.
- A rolling recent-history window balances continuity, prompt caching and the token limit.
  Shrinking, recounting and budget retries add latency when context is tight.
- **assistant-ui** gave me a decent web interface with little frontend work. **PydanticAI**
  let me customize memory without building the LLM call loop and low-level session handling
  myself; I chose it over DIY or LangGraph.

## What doesn't work / needs more work

Memory search is sometimes too slow. Slack integration and web-client connectivity have
received fixes, but I have not covered every failure mode. Large-document processing and
repeated context fitting can also delay replies.

## Time spent

Approximately **10 hours**: Monday ~1h, Tuesday ~1h, Wednesday ~3h, Thursday ~3h, Friday ~2h.
These are rough elapsed-time estimates with buffer for agents developing. I also worked on
other projects during those periods, so my actual focused time on this project was lower.

## Where the AI was wrong

1. **Forgetting deleted visible history.** Claude Code assumed forgetting should delete the
   actual session-history records. I caught this while inspecting test conversations: asking
   the agent to forget something should not remove that turn from the user's chat. Soft
   forgetting now preserves the display history while removing it from agent memory paths.
2. **Token counting used the wrong approach.** Claude Code, and possibly Codex, spent effort
   estimating input tokens and calibrating estimates against response usage. I caught this
   approach during review and asked for official token-count endpoints before inference,
   rather than further estimator tuning.

## AI suggestions I rejected

- Codex proposed tool search to reduce tool context. The current catalog is small; an extra
  discovery step could add confusion and latency for little token saving. Tools and their
  instructions are loaded upfront for now.
- I also rejected more elaborate session-history retrieval machinery aimed at individual
  scenarios. I preferred well-designed memory tools and clear instructions, retaining the
  session index for source retrieval without adding scenario-specific complexity.
