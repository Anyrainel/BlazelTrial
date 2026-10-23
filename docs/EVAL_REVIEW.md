# Evaluation review (2026-10-22 Pacific)

The existing acceptance suite is useful, but a PASS is evidence for a narrow core
workflow, not a production Slack/browser integration certification.

| Layer | Real components | Deliberate substitutions / limits |
|---|---|---|
| `python -m scenarios` | Configured provider, budget/count APIs, SQLite, core send/ingest, memory worker, search, note maintenance, document processing, output validation | Synthetic templated history/documents; Slack represented by a core thread ID. `send_message` records rather than delivers, as required by the assignment. |
| `pytest` | Real temporary database, core orchestration, storage, validators, HTTP TestClient | Network forbidden; scripted/TestModel responses, fixed notes, hash embeddings, mocked Slack API. These test contracts, not model intelligence or real Slack delivery. |
| `scripts/eval_retrieval.py` | Search over the selected persisted state | Fourteen fixed marker questions, no model query reformulation or answer checking; notes count if any covered source contains a marker. Broad repeated markers can make retrieval appear better than exact-event recall. |

## Findings and changes

- The seven original scenarios follow the assignment and use the shared core rather
  than injecting memory rows. Ingest intentionally skips agents, so synthetic
  assistant replies and history do not establish live memory-writing quality.
- The original locality check used the same configured model as a subjective judge.
  It could pass even if the local instruction was never followed. Replaced it with
  an exact draft-local canary, a positive check in A, and a leak check in B. This
  measures instruction isolation; it does not certify nuanced tone quality.
- Scenario 3 now requires a substantive product-specific summary as well as delayed
  date recall. This is a basic smoke check, not a full summary factuality rubric.
- Budget evidence now requires positive provider usage and API counting, checks the
  numeric exclusive limit independently of the logged boolean, and rejects empty
  evidence per scenario. Preflight refusals remain separate from sent calls.
- Summaries only count log entries appended during the current invocation. Invalid
  scenario selections and undersized long-history runs are rejected. A structured
  `eval-results.json` is saved beside runtime state, including actual model names
  (which can differ from configuration when provider fallback occurs).
- Added scenario 8: a correction must reach an already-open session that previously
  answered with the old value, while the old value remains historically answerable.
  This tests stale conversational context against updated shared memory.
- Added scenario 9: distinguish two events' caterers/deposits from a separate room
  fee after 40 ingested distractor turns. Facts arrive through live sends; exact
  checks reject borrowing a plausible value from the wrong event.

## Interpretation and remaining gaps

The suite uses shared state intentionally (notably history for scenarios 2/3/6/7).
Subset order and reused state can affect results; use a fresh default directory
for comparable runs. Fixed names/templates are small regression examples, not a
held-out generalization benchmark. Retrieval configuration comparisons need source
coverage validation, exact source/event labels, genuine fastembed rather than hash
embeddings, and a larger held-out query set before making architecture decisions.
The historical 12/14 score is insufficient evidence that vectors or notes never help.

No new real Slack messages were authorized or sent in this review. Real Slack
permissions, Socket Mode reconnects/status display, browser behavior, process-restart
persistence, sustained concurrent live sessions, document revisions and long
mutation/tool-compaction runs remain separate useful integration/stress checks.
Adding many near-duplicate recall prompts has less value than these boundary cases.

## Validation results

Live local Bonsai run (`.state/scenarios/20261022-184844`): scenario 5 PASS,
8 FAIL, 9 PASS; 52 provider calls, maximum 3,981 input tokens, no overshoot or
preflight refusal. Inspection confirmed a real stale-answer failure: A acknowledged
Marco Chen replacing Elena Voss and recalled Elena historically; the already-open
B still answered Elena as current. Keep this failure visible rather than relaxing
its assertion. This is a single run on a concurrently changing runtime, not a
provider-wide reliability estimate. Full original scenarios were not rerun.

Offline suite at review time: 198 passed, one failure in concurrently added
`tests/test_preferences.py` (`core.subagent_work` missing). The two new budget-check
regressions pass; scoped ruff and diff checks pass. Eval changes do not touch the
runtime model/memory files involved in that failure.
