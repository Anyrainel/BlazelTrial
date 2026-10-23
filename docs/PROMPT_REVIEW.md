# Hazel prompt review — 2026-10-22 Pacific

Reviewed all 13 model instruction files, tool docstrings and structured-output descriptions,
plus dynamic instruction assembly and tool feedback. Most prompts were already short. The
main issues were acceptance-scenario examples in tool schemas, repeated recall rules and
several conflicting instructions, rather than large narratives throughout the system.

| Location | Finding and change |
|---|---|
| `src/chatbot/prompts/system.md` | Removed quoted manager/plan questions, drafting examples and the placeholder example. Kept identity, direct drafting, latest-message precedence and completed-task boundaries. Consolidated detailed recall rules in the memory capability. |
| `src/chatbot/capabilities/memory/instructions.md` | Removed sample preference phrases, address requests and numbered source refs. Shortened procedural explanation; retained fresh recall, scopes, versioning, preference caps, forgetting and recovery. Explicitly qualify incomplete/conflicting results. |
| `src/chatbot/capabilities/memory/__init__.py` | Tool schemas contained Priya Raman and American/British spelling examples matching acceptance cases. Replaced them with general argument contracts; removed the sample address and arbitrary ref IDs. |
| `src/chatbot/capabilities/memory/worker.md` | Replaced explanatory prose and search examples with short evidence, scope, query and stopping rules. |
| `src/chatbot/capabilities/memory/worker_read.md` | Removed sample changing facts and a quoted acknowledgment. Kept version checks, context expansion, search-before-not-found and evidence requirements. |
| `src/chatbot/capabilities/memory/worker_write.md` | Removed American spelling and manager/spelling key examples. Retained duplicate detection, supersession, short imperative preferences, caps, scope and source citation. |
| `src/chatbot/capabilities/memory/worker_forget.md` | Replaced address-component examples with partial-value and alternate-wording rules; shortened the workflow. Kept broad-match authorization, verification and no-echo safeguards. Clarified soft-mode history retention. |
| `src/chatbot/capabilities/memory/subagent.py` | Removed arbitrary IDs and topic examples from model-facing field/tool descriptions. |
| `src/chatbot/capabilities/messaging/instructions.md` and `__init__.py` | Removed sample relationships and recipient names. Removed the stale-history exception to fresh recall. Tool description and confirmation now accurately say messages are recorded with simulated delivery. |
| `src/chatbot/capabilities/linkedin/instructions.md`, `writer.md` and `__init__.py` | Shortened the brief workflow and removed incidental examples. Replaced the obsolete preference-search requirement with the injected standing preferences, including in the tool schema. Preserved post limits and verbatim output. |
| `src/chatbot/capabilities/docs/__init__.py` | Removed the scenario-specific Q3-plan ID from the fetch tool description. Preserved the concurrently added document catalog tool. |
| `src/chatbot/capabilities/docs/instructions.md` | Already concise: discovery, pagination, bounded reading, detail search and exact facts. Retained. |
| `src/chatbot/prompts/doc_map.md`, `doc_reduce.md`, `summarize.md` | Already concise task/output contracts. Their detail categories describe what to preserve, not arbitrary sample content. Retained word limits, exact facts and source context. |

Kept literal API names, scope/status enums, character limits and ref formats: these specify
the interface. Generated source excerpts, document notes, user preferences and actual tool
errors are task evidence or operational feedback, not illustrative prompt prose. They remain.
No provider/model selection, memory storage or budget enforcement changed.

Validation: 206 offline tests pass. Live acceptance and budget results are recorded in
`WORKLOG.md` when the run completes. No new string-matching prompt tests were added; existing
behavior tests and real-model scenarios check the consequences of the rewrite.
