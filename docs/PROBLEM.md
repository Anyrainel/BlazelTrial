# Problem Statement

> Clean restatement of `assignment.md`. If the two ever disagree, `assignment.md` wins.
> Assumptions we make go in `NOTES.md`, not here.
> Authorized extension (2026-10-22 Pacific): the user explicitly expanded Slack
> beyond the original assignment's DM-only surface to channel @mentions. This
> extension takes precedence for channel behavior; the original assignment stays intact.

## 1. Goal

Build a simple chatbot for **one user** that is reachable from **two surfaces** and shares
**memory and context** between them, while every LLM call stays under a hard
**input-token budget**.

The real problem being tested is *memory and context management*: what to remember,
what to scope to one session, what to forget, and how to fit long histories and large
documents into a small context window.

## 2. Surfaces

| | Main chat | Slack DM / channel mentions |
|---|---|---|
| **Shape** | Many sessions, used in parallel. Each starts with fresh context. | One endless DM (`slack-dm`) plus one conversation per channel (`slack-<channel ID>`); hundreds of messages over months. |
| **Interface** | Our choice: CLI, HTTP, or bare web page. No styling. | A real Slack app in a free workspace we control. |

- Single user; no authentication. The main-chat user and the Slack user are the same
  person — a hard-coded mapping is fine.

## 3. Functional requirements

### R1 — Single core entry point
```
send(user_id, thread_id, text) -> reply
```
- `thread_id` is a session id, the literal `slack-dm`, or `slack-` plus the unique
  Slack channel ID. Channel Slack reply threads share that channel conversation.
- Slack accepts human DMs and explicit @Hazel channel mentions. Replies to channel
  mentions go under the request (or existing parent); each follow-up must mention
  Hazel again. DM threading remains unchanged.
- Shared long-term user facts are recallable across channels, DMs and existing main
  sessions. Draft-only/session instructions stay local. Validate this with accepted
  turns in each conversation, rather than treating ignored messages as isolation.
- Used by **both** surfaces. No surface-specific logic in the core.
- Must be callable by evaluators **from the command line or over HTTP**.

### R2 — Tools / capabilities
| Tool | Behaviour |
|---|---|
| `fetch_doc(doc_id)` | Reads a file from the `docs/` folder. |
| `send_message(to, text)` | Does not send; **logs** the call (must be inspectable by tests). |
| LinkedIn post writer | Output ≤ **1,300 characters**, ≤ **3 hashtags**, all hashtags **at the end**, **no links**. |

- Generate several documents of **20k–40k tokens** each, including **`Q3-plan`**, which
  states a **launch date**.
- **Extensibility:** assume ~20 more capabilities will be added later, each with its own
  instructions. Adding one must be cheap and must not blow the token budget (i.e. not all
  capability instructions can live in every prompt).

### R3 — Token budget
- Every model call stays **< 8,000 input tokens**, as reported by the **provider's usage field**.
- **Log the input-token count for every call.**
- The limit is **configurable**.

### R4 — State
- Persisted in **SQLite or files inside the repo directory**.

### R5 — Scenario script
- Runs all scenarios in §4 and prints **pass/fail** per scenario.
- May call the core directly with `slack-dm` instead of going through Slack.

## 4. Acceptance scenarios

| # | Name | Steps | Pass condition |
|---|---|---|---|
| 1 | **"I just told you."** | Open session B. In session A: *"My manager is Priya Raman."* Then ask *"Who's my manager?"* in session B **and** in Slack DM. | Both answer *Priya Raman*. (Cross-thread user facts propagate, even into a session opened earlier.) |
| 2 | **Long thread** | Load ~500 turns of synthetic Slack DM history **through the same code path as live messages**. Ask about a detail from ~turn 20. | Correct answer. |
| 3 | **Big document** | Slack DM: *"Summarize doc Q3-plan."* (doc > budget). 100 turns later: *"What was the launch date in that plan?"* | Correct launch date. |
| 4 | **Changed mind** | *"Always use British spelling."* … later *"Switch to American spelling."* | New drafts use American spelling; *"What did I say about spelling before?"* explains the change (history of the preference is kept). |
| 5 | **Stays local** | Session A: *"For this draft only, make it playful."* Session B requests a draft. | Session B's draft is **not** playful. (Ephemeral / session-scoped instructions don't leak.) |
| 6 | **Forget** | Home address mentioned early in Slack DM (~400 turns ago). *"Forget my address."* Then *"Message my partner where I live."* | Address absent from the reply, absent from every `send_message` call, **and gone from stored state** (incl. summaries, indexes, logs-as-state). |
| 7 | **LinkedIn post** | With scenario 2 history loaded, in Slack DM: *"Write a LinkedIn post announcing our Q3 launch, based on doc Q3-plan."* | All post rules pass; every call within budget. |

Evaluators will also run **their own variations** against the core — solutions must
generalise, not special-case these prompts.

## 5. Out of scope
Other channels (Teams, SMS…), authentication, UI polish, deployment.

## 6. Process requirements
- Use **both Claude Code and Codex**.
- Fresh directory used only for this task.
- **Commit as you go. The first commit is a short plan.**
- **Keep every AI session** — no trimming, curating, or restarting to hide dead ends.
- Unclear → make a reasonable assumption, record it in `NOTES.md`, keep going.
- Out of time → stop and document what's left and next steps. Honest partial > rushed complete.
- Budget: ~10–12 hours. Deadline: 5 days from receipt.

## 7. Deliverables
1. **Repo** — zip including `.git`, or GitHub link (if private, share with `@skblazel`).
2. **README.md** — how to run the core, the Slack app, the scenario script; how to add a capability.
3. **NOTES.md** (one page) — design, trade-offs, what doesn't work, approx. hours;
   2 places Claude Code/Codex was wrong and how it was caught; 1 rejected AI suggestion and why.
4. **AI transcripts**, unedited, API keys redacted, sent separately (**not committed**):
   - Claude Code: `~/.claude/projects/<project-path>/` incl. `subagents/`
   - Codex: `~/.codex/sessions/**/rollout-*.jsonl` for the working days
   - Any other AI tool: exported.
5. **Screen recording** ≤ 2 min: scenario 1 working in the real main chat and real Slack DM.

Follow-up: a 45-minute walkthrough of the sessions.
