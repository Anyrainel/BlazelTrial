"""Deterministic synthetic Slack DM history (~500 turns over ~4 months), no LLM needed.

Filler turns are built from templates. A few details are planted at fixed turns for the
scenarios (and evaluator-style variations) to ask about later.

    uv run python -m scenarios.fixtures.slack_history .state/fixtures/slack_history.jsonl
"""

import json
import random
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

START = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Planted:
    turn: int
    user: str
    assistant: str


# Scenario 2 asks about turn ~20; scenario 6 forgets the address from turn ~100.
PLANTED = {
    "caterer": Planted(
        20,
        "Booked the caterer for Dana's farewell: Saffron Table. They want a $340 deposit by "
        "Thursday, and the tasting is on June 18.",
        "Noted: Saffron Table for Dana's farewell, $340 deposit due Thursday, tasting June 18.",
    ),
    "partner": Planted(
        60,
        "My partner Sam is picking up the car from the shop today, so I'm working from home.",
        "Got it, hope the car is all fixed. Enjoy the quiet day at home.",
    ),
    "address": Planted(
        100,
        "We finally moved! New address is 27 Larkspur Lane, Apt 4B, Portland, OR 97214.",
        "Congratulations on the move! I've noted 27 Larkspur Lane, Apt 4B, Portland, OR 97214.",
    ),
}

_PEOPLE = ["Jordan", "Mei", "Arjun", "Lucia", "Tomasz", "Grace", "Omar", "Hana", "Felix", "Ines"]
_PROJECTS = ["billing revamp", "mobile onboarding", "data pipeline", "search ranking", "Q2 audit"]
_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
_FOODS = ["ramen", "a burrito bowl", "leftover pasta", "a poke bowl", "soup and bread"]
_BOOKS = ["a sci-fi novel", "a history of maps", "a cookbook", "a biography of a chess player"]

_FILLER = [
    (
        "Can you remind me what's on my plate for {day}?",
        "You mentioned the {project} review and a 1:1 with {person}.",
    ),
    ("Had {food} for lunch, feeling much better now.", "Glad to hear it!"),
    (
        "{person} pinged me about the {project} again.",
        "Want me to draft a quick status reply to {person}?",
    ),
    (
        "Draft a two-line update on the {project}: on track, demo next {day}.",
        "The {project} is on track. We'll demo it next {day}.",
    ),
    ("Ugh, the standup ran 40 minutes today.", "That's long. Maybe suggest a timebox?"),
    ("Started reading {book}, it's surprisingly good.", "Nice pick! Enjoy it."),
    ("Gym at 7 tomorrow, remind me to pack shoes.", "Will do: pack gym shoes for 7am."),
    (
        "What's a good way to say no to an extra meeting politely?",
        "Try: 'I'd love to help, but I'm at capacity this week. Can we revisit next week?'",
    ),
    ("The {project} deadline moved to the {n}th.", "Noted, the {project} is now due the {n}th."),
    ("{person} is out sick, I'm covering their reviews.", "Thanks for stepping in."),
    ("Rainy again. Third day in a row.", "Sounds like a good day for focused work."),
    ("Booked a dentist cleaning for {day} at {hour}:30.", "Got it, dentist {day} at {hour}:30."),
    ("Any tips for keeping slides short?", "One idea per slide, and no more than six lines."),
    (
        "Coffee with {person} went well, they might join the {project}.",
        "Great news for the {project}!",
    ),
]


def generate(n_turns: int = 500, seed: int = 7) -> list[dict]:
    rng = random.Random(seed)
    planted_by_turn = {p.turn: p for p in PLANTED.values()}
    rows, ts = [], START
    for turn in range(1, n_turns + 1):
        ts += timedelta(minutes=rng.randint(20, 600))
        if turn in planted_by_turn:
            p = planted_by_turn[turn]
            user, assistant = p.user, p.assistant
        else:
            user_t, assistant_t = rng.choice(_FILLER)
            slots = {
                "person": rng.choice(_PEOPLE),
                "project": rng.choice(_PROJECTS),
                "day": rng.choice(_DAYS),
                "food": rng.choice(_FOODS),
                "book": rng.choice(_BOOKS),
                "n": rng.randint(2, 28),
                "hour": rng.randint(8, 17),
            }
            user, assistant = user_t.format(**slots), assistant_t.format(**slots)
        rows.append({"user": user, "assistant": assistant, "ts": ts.isoformat()})
    return rows


def write(path: Path, n_turns: int = 500) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in generate(n_turns):
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


if __name__ == "__main__":
    out = write(Path(sys.argv[1] if len(sys.argv) > 1 else ".state/fixtures/slack_history.jsonl"))
    print(f"wrote {out}")
