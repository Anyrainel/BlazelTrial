# Hazel Slack evaluation — 2026-10-21 (Pacific)

Evaluated the existing build in the open blazeltrial workspace, using Hazel DMs
and #test. No production code was changed. The bot was not running initially;
started it with `uv run chatbot slack` using the configured Claude model.
Native Slack input was unreliable (window activation/bounds errors), so actual
message tests used the same signed-in workspace in Chrome.

## Findings

### P1 — Failed events are marked completed before processing succeeds

`src/chatbot/surfaces/slack_app.py:42` calls `first_time()` before `send_async()`.
That immediately inserts the event ID into the process-local seen set. A failed
model request therefore prevents the same event from being processed again.
The outer listener posts a generic error, but cannot recover the original event.

Deterministic offline reproduction: patch `slack_app.send_async` with an
`AsyncMock(side_effect=RuntimeError(...))`; call `handle_dm` with a plain DM and
event ID `qa-failure-742`, catch the exception, then repeat that event. Result:
second reply is `None`, core invocation count remains 1.

Delivery also has no durable recovery: `await say(reply)` is outside the exception
handler, after the core has persisted the answer. A Slack posting failure leaves
a completed answer in SQLite that the user never sees. This second failure path
is established by inspection; no real Slack outage was induced.

Recommendation: explicit pending/completed/failed event state plus a persisted
reply/outbox, with retries that resend a completed answer without rerunning tools.
The current 1,000-entry in-memory cache also loses deduplication on restart.

### P2 — Rapid DM requests run concurrently and reply out of order

Live reproduction: send a request for a 350-word story beginning `SLOW-742`, then
immediately request exactly `FAST-742`. Both user messages arrived within 0.22 s.
The FAST response appeared first (about 2 s); the SLOW response arrived about
10 s after its request. Both answers were top-level messages, with no indication
of which request they answered beyond the test markers.

Evidence: [first request](https://blazeltrial.slack.com/archives/D0CACNKNE1Z/p1792649826287869),
[FAST answer](https://blazeltrial.slack.com/archives/D0CACNKNE1Z/p1792649828012429),
[SLOW answer](https://blazeltrial.slack.com/archives/D0CACNKNE1Z/p1792649836562379).

`handle_dm` directly awaits the core; `core.send_async` has no per-conversation
queue or lock. A later turn can run before the earlier answer or memory mutation
finishes. The observed defect is reply ordering; memory corruption was not proven.
Serialize complete turns for the endless Slack conversation and provide bounded
queue/progress feedback.

### P2 — Common Markdown is posted without Slack formatting conversion

Requested a bold heading, a two-row Markdown table, and a labelled link. Hazel
posted literal `**Project Status Overview**`, pipe table syntax, and
`[Example](https://example.com)`. Slack auto-linked the URL but displayed the
Markdown label syntax. Plain italic text in the story did render.

[Formatting response](https://blazeltrial.slack.com/archives/D0CACNKNE1Z/p1792649801995409).
The surface sends `say(reply)` unchanged. Add a surface formatter for supported
Slack markup; represent tables as readable lists or appropriate blocks.

### Scope limitation — Channel mentions are silently unsupported

Hazel was initially absent from #test. Added it using `/invite @Hazel`, verified
membership, and sent a real mention requesting `CHANNEL-JOINED-742`. No answer.
`should_handle()` explicitly accepts only `channel_type == "im"`, and the app
registers no `app_mention` handler. This is consistent with the assignment's
DM-only requirement, not a failure of that requirement. It does prevent using
#test as a conversation surface; document the limitation or add an explicit
mention handler if channel testing is intended.

[Post-join mention](https://blazeltrial.slack.com/archives/C0CB55JKB7Y/p1792649920645169).

## Passed checks

- Live DM smoke test: exact `HAZEL-READY-742`, approximately 4 s.
- Live memory write: explicitly saved a fictional project codename.
- Cross-surface recall: a new CLI thread `qa-slack-cross-742` correctly recalled
  the fact saved in Slack.
- Live Slack thread reply: `THREAD-742` appeared under the correct parent,
  without appearing as an extra top-level message. Bolt's `say` preserves routing.
- Full offline suite: `uv run pytest -q` — 116 passed, 19.55 s.
- Live forget: asked Hazel to delete the fictional codename. It confirmed after
  approximately 17 s; a byte search of `.state/state.db` found no occurrence of
  the test value afterwards. Original Slack messages remain visible; local
  forgetting does not delete the Slack-hosted conversation.
- This evaluation window logged 26 model calls (Slack plus cross-surface recall),
  maximum input 5,269 tokens, zero provider-reported budget violations.

## Other inspected limitations

- File-share messages, edits, and other subtypes are silently ignored, including
  file captions, because every nonempty `subtype` is rejected. File ingestion
  was not a requirement and no real file was uploaded.
- No typing/progress indicator or explicit request timeout in the adapter.
- Opening the app generates unhandled `app_home_opened` warnings/404 execution
  results. This did not prevent messages from working.
- `SLACK_ALLOWED_USER` is unset in the current configuration. As documented, all
  DM authors map to the same user and same history. Restrict the existing setting
  before admitting other workspace users; no second-user privacy test was run.

The live transcript remains in Slack as reproducible evidence. No existing
messages or user data were deleted. This was targeted integration testing, not a
rerun of all seven long-history acceptance scenarios through Slack.

Screenshot evidence is retained locally in `.state/logs/slack-qa-742.png`
(gitignored). The Slack bot process is left running for continued testing.
