"""Scenario script: runs the 7 assignment scenarios against the real core and prints PASS/FAIL.

    uv run python -m scenarios                 # all, in a fresh state dir under .state/scenarios/
    uv run python -m scenarios --only 1,4,5    # a subset
    uv run python -m scenarios --state-dir X   # reuse/inspect a specific state dir

Calls `core.send` / `core.ingest` directly (thread "slack-dm" stands in for Slack). Every
model call goes through the budget guard; the summary reports the largest reported input.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--only", default="1,2,3,4,5,6,7,8,9", help="comma list of scenario numbers")
    p.add_argument("--state-dir", default=None, help="state dir (default: fresh under .state/)")
    p.add_argument("--turns", type=int, default=500, help="synthetic Slack history length")
    p.add_argument("--quiet", action="store_true", help="don't print the conversation")
    return p.parse_args()


ARGS = parse_args()
STATE = Path(
    ARGS.state_dir or REPO / ".state" / "scenarios" / datetime.now().strftime("%Y%m%d-%H%M%S")
)
os.environ["STATE_DIR"] = str(STATE)  # before chatbot reads its settings
os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
sys.path.insert(0, str(REPO))

from chatbot.capabilities.linkedin import validator  # noqa: E402
from chatbot.capabilities.messaging import sent_messages  # noqa: E402
from chatbot.config import get_settings  # noqa: E402
from chatbot.core import SLACK_THREAD_ID, flush, ingest, send  # noqa: E402
from chatbot.memory.grep import find  # noqa: E402
from chatbot.memory.messages import recent_history, thread_messages  # noqa: E402
from chatbot.memory.records import MemoryCaller, search_memory  # noqa: E402
from scenarios.checks import budget_problems  # noqa: E402
from scenarios.fixtures.docs import Q3_LAUNCH_DATE, write_all  # noqa: E402
from scenarios.fixtures.slack_history import PLANTED, generate  # noqa: E402

DM = SLACK_THREAD_ID
BRITISH = re.compile(r"\b(colour|favourite|organis|realis|centre|behaviour|recognis)", re.I)
AMERICAN = re.compile(r"\b(color|favorite|organiz|realiz|center|behavior|recogniz)", re.I)
DRAFT = (
    "Write two sentences for the team about our favourite colours for the new office "
    "and how we'll organise the move."
)
results: list[tuple[str, bool, str]] = []
_history_loaded = False


def say(thread: str, text: str) -> str:
    reply = send("me", thread, text)
    if not ARGS.quiet:
        print(f"  [{thread}] you> {text}\n  [{thread}] bot> {reply}\n", flush=True)
    return reply


def log_rows() -> list[dict]:
    path = get_settings().llm_log_path
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


def load_history() -> None:
    global _history_loaded
    if _history_loaded:
        return
    t0 = time.time()
    for row in generate(ARGS.turns):
        ingest("me", DM, row["user"], row["assistant"], datetime.fromisoformat(row["ts"]))
    flush("me", DM)
    _history_loaded = True
    print(
        f"  (loaded {ARGS.turns} turns of Slack history in {time.time() - t0:.0f}s)\n", flush=True
    )


# --- scenarios ----------------------------------------------------------------------------------


def s1() -> None:
    say("s1-B", "Hi! I'll have a question for you in a bit.")  # session B opened earlier
    say("s1-A", "My manager is Priya Raman.")
    b = say("s1-B", "Who's my manager?")
    d = say(DM, "Who's my manager?")
    record("1 I just told you", "Priya Raman" in b and "Priya Raman" in d)


def s2() -> None:
    load_history()
    caterer = PLANTED["caterer"]
    r = say(DM, "Which caterer did I book for Dana's farewell, and how much was the deposit?")
    record(f"2 Long thread (turn {caterer.turn})", "Saffron Table" in r and "340" in r)


def s3() -> None:
    load_history()
    write_all(get_settings().docs_path)
    summary = say(DM, "Summarize doc Q3-plan.")
    summary_ok = "lumen" in summary.lower() and len(summary.split()) >= 30
    for row in generate(100, seed=99):
        ingest("me", DM, row["user"], row["assistant"])
    flush("me", DM)
    r = say(DM, "What was the launch date in that plan?")
    launch = re.search(r"\bSept(?:ember|\.)?\s+29\b", r, re.I)  # "Sept 29" is the same date
    record(
        "3 Big document (+100 turns)",
        summary_ok and bool(launch) and "2026" in r,
        f"substantive summary={summary_ok}",
    )


def s4() -> None:
    t = "s4"
    say(t, "Always use British spelling.")
    d1 = say(t, DRAFT)
    say(t, "Switch to American spelling.")
    d2 = say(t, DRAFT)
    h = say(t, "What did I say about spelling before?")
    ok = (
        bool(BRITISH.search(d1))
        and bool(AMERICAN.search(d2))
        and not BRITISH.search(d2)
        and "british" in h.lower()
        and "american" in h.lower()
    )
    record("4 Changed mind", ok, f"d1 british={bool(BRITISH.search(d1))} d2 american-only={ok}")


def s5() -> None:
    marker = "JELLYFISH-CONFETTI-742"
    a = say(
        "s5-A",
        f"For this draft only, end the invitation with the exact phrase {marker}. "
        "Write a two-sentence invite to the team's Friday lunch.",
    )
    d = say("s5-B", "Write a two-sentence invite to the team's Friday lunch.")
    record(
        "5 Stays local",
        marker in a and marker not in d,
        f"local instruction followed={marker in a}; leaked={marker in d}",
    )


def s6() -> None:
    load_history()
    address = PLANTED["address"].user.split("is ", 1)[1].rstrip(".")
    marks = ["Larkspur", "97214", "Apt 4B"]
    present_before = bool(find("me", "Larkspur").total())
    before = len(sent_messages())
    r1 = say(DM, "Forget my address.")
    r2 = say(DM, "Message my partner where I live.")
    sent = sent_messages()[before:]
    in_text = any(m.lower() in (r1 + r2).lower() for m in marks)
    in_sent = any(m.lower() in s.text.lower() for s in sent for m in marks)
    # Memory, not the chat log: the address must be unreachable from every memory path (records,
    # notes, saved outputs, sent messages, search indexes, replay) while the chat history keeps
    # the conversation, flagged `forgotten`.
    reachable = [m for m in marks if find("me", m).total()]
    indexed = [m for m in marks if search_memory(MemoryCaller("me", DM), m)]
    replayed = [
        m
        for m in marks
        if m.lower()
        in str(recent_history(DM, budget=10_000, step=1, chars_per_token=3.0).messages).lower()
    ]
    kept = [m for m in thread_messages(DM) if m.forgotten and "Larkspur" in m.text]
    ok = (
        present_before
        and not in_text
        and not in_sent
        and not (reachable or indexed or replayed)
        and bool(kept)
    )
    record(
        "6 Forget",
        ok,
        f"present_before={present_before} reply={in_text} send_message={in_sent} "
        f"memory={reachable + indexed + replayed} "
        f"chat_kept_flagged={len(kept)} ({address[:12]}…)",
    )


def s7() -> None:
    load_history()
    write_all(get_settings().docs_path)
    n0 = len(log_rows())
    post = say(DM, "Write a LinkedIn post announcing our Q3 launch, based on doc Q3-plan.")
    rows = [r for r in log_rows()[n0:] if r["input_tokens"] is not None]  # calls actually sent
    problems = validator.problems(post)
    within = all(r["within_budget"] for r in rows)
    ok = not problems and within and Q3_LAUNCH_DATE.split(",")[0] in post
    record(
        "7 LinkedIn post",
        ok,
        f"chars={len(post)} problems={problems} calls={len(rows)} within={within}",
    )


def s8() -> None:
    say("s8-B", "Hello, I'll ask about reporting lines later.")
    say("s8-A", "My manager is Elena Voss. Remember this for future conversations.")
    old = say("s8-B", "Who is my current manager?")
    say(
        "s8-A",
        "My manager changed: now it is Marco Chen, replacing Elena Voss. Remember the change.",
    )
    current = say("s8-B", "Who is my current manager? Give only their name.")
    history = say("s8-A", "Who was my manager before Marco Chen?")
    record(
        "8 Cross-session correction",
        "Elena Voss" in old
        and "Marco Chen" in current
        and "Elena Voss" not in current
        and "Elena Voss" in history,
        f"initial={old!r}; current={current!r}; previous={history!r}",
    )


def s9() -> None:
    t = "s9-distractors"
    for text in (
        "For the Cedar workshop, the caterer is Juniper Kitchen and the deposit is $275.",
        "For the Willow workshop, the caterer is Copper Spoon and the deposit is $610.",
        "For the Cedar workshop, the room rental is $480. This is separate from catering.",
    ):
        say(t, text)
    for row in generate(40, seed=193):
        ingest("me", t, row["user"], row["assistant"])
    flush("me", t)
    answer = say(
        t,
        "Which caterer is booked for the Cedar workshop, and what is its deposit? "
        "Give only the caterer and deposit.",
    )
    record(
        "9 Distractor discrimination",
        "Juniper Kitchen" in answer
        and "275" in answer
        and not any(value in answer for value in ("Copper Spoon", "610", "480")),
    )


SCENARIOS = {"1": s1, "2": s2, "3": s3, "4": s4, "5": s5, "6": s6, "7": s7, "8": s8, "9": s9}


def main() -> int:
    print(f"State dir: {STATE}\nModel: {get_settings().llm_provider}:{get_settings().llm_model}\n")
    selected = [k.strip() for k in ARGS.only.split(",") if k.strip()]
    if not selected or any(k not in SCENARIOS for k in selected):
        raise SystemExit("--only must contain scenario numbers 1 through 9")
    if any(k in selected for k in ("2", "3", "6", "7")) and ARGS.turns < 500:
        raise SystemExit("Long-history acceptance scenarios require --turns >= 500")
    baseline = len(log_rows())
    started = datetime.now(UTC)
    for key in selected:
        print(f"--- scenario {key}", flush=True)
        before_calls = len(log_rows())
        try:
            SCENARIOS[key]()
            issues = budget_problems(log_rows()[before_calls:], get_settings().max_input_tokens)
            if issues:
                record(f"{key} budget evidence", False, "; ".join(issues))
        except Exception as exc:  # a crash is a failure, not the end of the run
            record(f"{key} (crashed)", False, f"{type(exc).__name__}: {exc}")
    all_rows = log_rows()[baseline:]
    # The budget rule is about calls actually sent (provider-reported usage). Requests the
    # guard refused at pre-flight were never sent; they are reported separately.
    refused = [r for r in all_rows if r["error"] == "preflight_over_budget"]
    rows = [r for r in all_rows if r["input_tokens"] is not None]
    over = [r for r in rows if not r["within_budget"]]
    print("\n=== Summary")
    for name, ok, _detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    print(
        f"\nModel calls: {len(rows)}; max reported input tokens: "
        f"{max((r['input_tokens'] or 0) for r in rows) if rows else 0} "
        f"(limit {get_settings().max_input_tokens}); over budget: {len(over)}; "
        f"refused before sending (then retried smaller): {len(refused)}; "
        f"elapsed: {(datetime.now(UTC) - started).seconds}s"
    )
    issues = budget_problems(all_rows, get_settings().max_input_tokens)
    budget_ok = not issues
    if issues:
        print(f"Budget evidence failures: {issues}")
    print(f"{'PASS' if budget_ok else 'FAIL'}  every sent call within budget")
    report = {
        "model": f"{get_settings().llm_provider}:{get_settings().llm_model}",
        "actual_models": sorted({r["model"] for r in rows}),
        "state_dir": str(STATE.resolve()),
        "results": [dict(name=name, passed=ok, detail=detail) for name, ok, detail in results],
        "budget_problems": issues,
        "calls": len(rows),
        "max_input_tokens": max((r["input_tokens"] for r in rows), default=0),
    }
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "eval-results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if results and all(ok for _, ok, _ in results) and budget_ok else 1


if __name__ == "__main__":
    sys.exit(main())
