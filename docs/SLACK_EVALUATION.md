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

## Cross-surface and cross-conversation follow-up — 2026-10-21 Pacific

Used real Slack DMs/reply threads, #test, and two CLI sessions sharing the same
single-user identity. Session B was opened before A saved the test fact. These
CLI calls exercise the main-chat core; the web and HTTP UI were not exercised.

| Check | Result | Evidence |
|---|---|---|
| Main A fact -> already-open main B | PASS | B recalled the fictional coordinator saved later in A. |
| Main A fact -> live Slack DM | PASS | Slack recalled the coordinator without the name appearing in the question. |
| A draft-only prefix -> B and Slack | PASS | Neither destination used A's one-off prefix in its requested announcement. |
| Slack updates shared fact -> existing A and B | PASS | Both recalled current and previous names correctly. |
| Version history | PASS | Read-only DB inspection confirmed a two-record supersession chain, with origins A then slack-dm. |
| Top-level DM shared fact -> Slack reply thread | PASS | Thread reply used current and previous names and stayed under its parent. |
| Thread draft-only prefix -> next top-level DM and main B | PASS before forget | Neither subsequent announcement used the thread's one-off prefix. |
| #test-only fact -> DM | UNSUPPORTED | No channel reply; DM explicitly said it could not access the label; no message row contained the channel-only sentinel. |
| Main B forget -> existing main A and Slack reply thread | PASS for deletion/recall | Both stopped recalling names; DB byte search found neither full name. See the separate behavioral regression below. |

Initial main-chat and all Slack calls used Claude Sonnet 5.5. The repository's
provider setting changed to local Bonsai 2 during the evaluation; newly started
CLI calls for updated-fact recall and forgetting used Bonsai. Logs establish the
provider of each call. Those results demonstrate shared persistence across
providers, but are not a controlled same-model comparison. No provider setting
was changed by this evaluation.

Channel membership was independently checked with `/invite @Hazel`: Slackbot
reported that Hazel was already in #test. A channel-only synthetic deployment
label still never entered the core. Thus channel-to-channel memory cannot be
validated through this adapter until channel handling exists. Silence here must
not be scored as successful channel isolation.

### New P2 — Forgetting revives an old draft-only instruction

Live sequence:

1. In a Slack reply thread request a release draft with prefix `THREAD-LOCAL-853`,
   explicitly **for this draft only**, including the shared coordinator names.
2. Ask for another release draft in the top-level DM. It correctly omits the prefix.
3. From main session B, forget both coordinator names across sessions.
4. Back in the existing Slack reply thread, ask only who the current and previous
   coordinators are, explicitly asking it not to guess.

The answer correctly says the names are unavailable, but then appends an
unrequested release announcement starting with `THREAD-LOCAL-853`. This is a
failure of the one-off instruction lifetime, even though the names were deleted.

[Thread instruction](https://blazeltrial.slack.com/archives/D0CACNKNE1Z/p1792651257477639?thread_ts=1792651120.216159&cid=D0CACNKNE1Z),
[leaked prefix after forget](https://blazeltrial.slack.com/archives/D0CACNKNE1Z/p1792651368262489?thread_ts=1792651120.216159&cid=D0CACNKNE1Z).

Read-only DB inspection after forgetting showed user requests in turns 8, 11 and
12 still present, but their assistant replies removed because they contained the
forgotten names. `memory/messages.py:_turn_messages` replays those orphan user
requests as user prompts. This is a plausible mechanism for reviving apparently
unfinished requests; causality needs a focused offline regression test. Retain
safe completion metadata or a neutral completion marker after deletion so replay
does not treat old fulfilled instructions as outstanding work. Never retain the
forgotten values to solve this.

A subsequent fresh top-level draft omitted the prefix, so the observed regression
is the post-forget thread answer above, not a claim that every later draft leaks.

The single endless `slack-dm` conversation intentionally shares memory between
top-level messages and Slack reply threads. They are not independent sessions.
No second Slack user, second workspace, private channel, or multi-user isolation
was tested. The tests used synthetic data only.

Follow-up validation: 119 offline tests passed in 21.84 s (the suite grew during
concurrent repository work). The follow-up window contained 122 model calls for
the two QA sessions and Slack, max provider input 7,144 tokens, no provider budget
violations or pre-flight refusals. Screenshot:
`.state/logs/slack-cross-qa-853.png` (gitignored). Coordinator test facts were
forgotten through the product; Slack-hosted messages remain as evidence.
