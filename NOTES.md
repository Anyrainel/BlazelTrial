# NOTES

All runtime files live in `.state/`. The `docs/` folder Hazel reads is `.state/docs/`;
the repository-root `docs/` contains documentation produced during development.

Implementation decisions and detailed validation: [docs/DECISIONS.md](docs/DECISIONS.md)
and [docs/WORKLOG.md](docs/WORKLOG.md).

## Design

- **Shared core.** CLI, HTTP/web and Slack DMs/channel mentions all call
  `send(user_id, thread_id, text)`. History ingestion follows the same path.
- **Bounded context.** Each request combines stable instructions and tools with current
  preferences and recent conversation history. History rolls forward in chunks to preserve
  prompt-cache reuse, keeping tool calls and their results together.
- **Count before calling.** Every live request is checked against the configurable 8,000-token
  limit using provider count endpoints, or the local server's template/tokenizer endpoints.
  Oversized requests are shortened and recounted; failed counting blocks inference.
  Actual input usage, including cached tokens, is logged for every call.
- **Scoped memory.** SQLite stores LLM-written notes and records classified as facts,
  preferences or tasks, linked to their sources. Long-term records are shared across
  conversations; session records stay local. Current preferences are always included in
  context within size caps. Changes create new versions, preserving earlier preferences.
- **Recall through tools.** `ask_memory` uses a subagent with its own prompt and context to
  search records, notes, document passages and indexed session history. Exact text lookup
  and source reads recover details that summaries may miss.
- **Forget without erasing the conversation.** Soft forgetting preserves visible chat but
  excludes flagged messages from every agent memory path. Derived memory, saved outputs and
  index entries are deleted. Hard deletion is also available.
- **Bounded capabilities.** Large documents are read in chunks and summarized into cached
  notes. LinkedIn length, hashtag and link rules are enforced in code.

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
