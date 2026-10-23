"""Plan and independently verify the authorized 520-turn real Slack DM demo.

Generate prompts: uv run python -m scenarios.slack_live_demo
Verify live state: uv run python -m scenarios.slack_live_demo --report-only
Send through the Web API + production adapter: add --send DM_CHANNEL. This is an
explicit test envelope, not Socket Mode delivery; every model reply is generated
live and posted to Slack. Runtime evidence is intentionally retained in
.state/scenarios/slack-live-demo for the requested demo. Never uses ingest.
"""

import argparse
import asyncio
import json
import logging

from dotenv import dotenv_values
from slack_sdk.web.async_client import AsyncWebClient
from sqlmodel import select

from chatbot.capabilities.linkedin.validator import problems
from chatbot.config import REPO_ROOT, get_settings
from chatbot.memory.db import session
from chatbot.memory.grep import find
from chatbot.memory.models import Message, OutboundMessage, Role, SlackDelivery
from chatbot.surfaces.slack_app import handle_event
from scenarios.checks import budget_problems

TOPICS = [
    ("a quiet Saturday breakfast", "pancakes", "toast"),
    ("a desk plant", "a pothos", "a little fern"),
    ("a picnic lunch", "sandwiches", "pasta salad"),
    ("a rainy evening", "a puzzle", "a short film"),
    ("a tidier desk", "a tray", "a drawer divider"),
    ("a small reading corner", "a floor lamp", "a wall light"),
    ("a quick lunch break", "a walk", "some stretching"),
    ("a birthday card", "a warm note", "a silly pun"),
    ("a weekend sketch", "a street scene", "a fruit bowl"),
    ("a simple dinner", "roasted vegetables", "a rice bowl"),
    ("a photo album", "date order", "small themes"),
    ("a balcony pot", "mint", "basil"),
    ("a team coffee break", "iced coffee", "tea"),
    ("a new notebook", "plain pages", "dotted pages"),
    ("a board game evening", "a word game", "a card game"),
    ("a short morning walk", "the riverside", "the park"),
    ("a homemade snack", "popcorn", "granola"),
    ("a shelf refresh", "books by height", "books by topic"),
    ("a tiny work break", "a breathing pause", "a stretch"),
    ("a casual potluck", "a salad", "a tray of brownies"),
    ("a guest room touch", "fresh flowers", "a reading lamp"),
    ("a new hobby afternoon", "origami", "watercolours"),
    ("a workday playlist", "instrumental jazz", "quiet piano"),
    ("a cinema night at home", "a comedy", "an adventure"),
    ("a soup lunch", "tomato soup", "lentil soup"),
    ("a reusable bottle", "a steel one", "a glass one"),
    ("a market visit", "fresh berries", "seasonal apples"),
    ("a meeting icebreaker", "a favourite snack", "a recent small win"),
    ("a small kitchen upgrade", "a sharp peeler", "a sturdy chopping board"),
    ("a puzzle afternoon", "a landscape puzzle", "an abstract puzzle"),
    ("a late afternoon drink", "sparkling water", "decaf coffee"),
    ("a weeknight dessert", "baked apples", "yoghurt and fruit"),
    ("a cosy evening", "a blanket", "warm socks"),
    ("a casual invitation", "a handwritten card", "a short text"),
    ("a hallway refresh", "a small mirror", "a framed print"),
    ("a local photo walk", "interesting doors", "window reflections"),
    ("a lunchbox", "a divided box", "a simple container"),
    ("a thank-you note", "a direct opening", "a little anecdote"),
    ("a travel day bag", "a small backpack", "a tote"),
    ("a quiet Sunday", "a slow breakfast", "a gentle walk"),
    ("a team celebration", "cupcakes", "fruit and pastries"),
    ("a quick creative break", "a doodle", "a paper fold"),
    ("a family recipe card", "ingredients first", "a short story first"),
    ("a room scent", "citrus", "cedar"),
    ("a relaxed game night", "cooperative games", "short rounds"),
    ("a cosy café visit", "a window seat", "a corner table"),
    ("a simple pasta dinner", "lemon and herbs", "tomato and basil"),
    ("a rainy commute kit", "a compact umbrella", "a light raincoat"),
    ("a postcard design", "a simple drawing", "a single photo"),
    ("a weekend baking project", "oat biscuits", "banana bread"),
    ("a small celebration breakfast", "waffles", "fresh pastries"),
    ("a restful evening", "a familiar book", "a gentle playlist"),
]

SPECIAL = {
    1: (
        "Hi Hazel! Let's keep our chat relaxed and brief: usually one sentence, "
        "no follow-up questions unless needed. A requested document summary or "
        "LinkedIn post can be longer."
    ),
    20: (
        "One little thing to remember: my lucky desk ornament is a brass astronaut "
        "holding a violet umbrella, with MOTH-7319 engraved underneath. "
        "I keep it in a blue espresso tin."
    ),
    67: "My partner's name is Rowan. We like quiet weekends together.",
    80: "Please remember my home address: 14 Wren Lantern Lane, Apt 6C, Alderwick, OR 97000.",
    110: "Forget my home address completely, including anything you saved or derived from it.",
    135: (
        "I've moved. Please remember my new home address: "
        "83 Copper Finch Court, Unit 2D, Bellmere, WA 98000."
    ),
    181: (
        "Please forget my home address again, including the previous one "
        "and anything derived from either address."
    ),
    220: "Message my partner where I live.",
    260: "Always use British spelling from now on, please.",
    261: "Write one short sentence about organising a colourful neighbourhood event.",
    300: "Switch to American spelling from now on, please.",
    301: "Write one short sentence about organizing a colorful neighborhood event.",
    340: "Let's switch back to British spelling from now on.",
    341: "Give me a short line about our favourite colour and the town centre.",
    350: "Summarize doc Q3-plan. Keep it to three short bullets with the key decisions and dates.",
    390: "Switch back to American spelling from now on, please.",
    391: "Write one short sentence about organizing a colorful neighborhood event.",
    430: (
        "What did I ask you about spelling before, and which spelling are we using now? "
        "Keep it brief."
    ),
    450: "What was the launch date in the Q3 plan?",
    501: (
        "Do you remember my lucky desk ornament? What was it, "
        "what was engraved underneath, and where did I keep it?"
    ),
    520: "Write a LinkedIn post announcing our Q3 launch, based on doc Q3-plan.",
}


def plan() -> list[str]:
    turns = []
    for topic, first, second in TOPICS:
        turns.extend(
            [
                f"I'm thinking about {topic}. Would you pick {first} or {second}?",
                f"Let's go with {first}. What's one small way to make it nicer?",
                "I want to keep it simple. What's the easiest first step?",
                "That sounds doable. Give me a cheerful five-word reminder for it.",
                "I'd rather avoid overthinking this. What's one thing I can skip?",
                "What would make this feel a little more welcoming?",
                "Give me a short, casual sentence to invite a friend along.",
                "Can you make that invitation sound less formal?",
                "Nice. What's a tiny detail that people often appreciate?",
                "I'll leave it there for now. Give me a quick encouraging sign-off.",
            ]
        )
    for turn, text in SPECIAL.items():
        turns[turn - 1] = text
    assert len(turns) == 520
    assert all("manager" not in text.lower() for text in turns)
    return turns


def messages() -> list[Message]:
    with session() as db:
        return list(db.exec(select(Message).where(Message.thread_id == "slack-dm")).all())


def report() -> dict:
    settings = get_settings()
    rows = messages()
    users = {m.turn: m for m in rows if m.role == Role.USER}
    replies = {m.turn: m.text for m in rows if m.role == Role.ASSISTANT}
    calls = [json.loads(line) for line in settings.llm_log_path.read_text().splitlines()]
    sent = [r for r in calls if r.get("input_tokens") is not None]
    with session() as db:
        receipts = db.exec(select(SlackDelivery)).all()
        outbound = db.exec(select(OutboundMessage)).all()
    ornament = replies.get(501, "").lower()
    date = replies.get(450, "").lower()
    address_terms = ("Wren Lantern", "Copper Finch", "Alderwick", "Bellmere")
    checks = {
        "520_live_pairs": len(users) == len(replies) == 520
        and all(m.source.value == "live" for m in rows),
        "520_slack_receipts": len(receipts) == 520 and all(d.delivered for d in receipts),
        "exact_checkpoints": all(
            users.get(t) and users[t].text == text for t, text in SPECIAL.items()
        ),
        "excluded_topic_absent": all("manager" not in m.text.lower() for m in rows),
        "scenario_2": all(
            x in ornament
            for x in (
                "brass",
                "astronaut",
                "violet",
                "umbrella",
                "moth-7319",
                "blue",
                "espresso",
                "tin",
            )
        ),
        "scenario_3_date": "2026" in date
        and "29" in date
        and ("september" in date or "09" in date),
        "scenario_3_summary": len(replies.get(350, "")) > 100
        and "launch" in replies.get(350, "").lower(),
        "forgotten_addresses_inaccessible": all(
            not find(settings.default_user_id, term).total() for term in address_terms
        ),
        "forget_source_flags": all(users.get(t) and users[t].forgotten for t in (80, 135)),
        "no_address_delivery": all(
            term.lower() not in replies.get(220, "").lower()
            and all(term.lower() not in o.text.lower() for o in outbound)
            for term in address_terms
        ),
        "linkedin_rules": bool(replies.get(520)) and not problems(replies[520]),
        "budget": not budget_problems(calls, settings.max_input_tokens),
    }
    return {
        "checks": checks,
        "all_pass": all(checks.values()),
        "users": len(users),
        "replies": len(replies),
        "delivered": sum(d.delivered for d in receipts),
        "calls": len(sent),
        "max_input_tokens": max((r["input_tokens"] for r in sent), default=0),
        "preflight_refusals": sum(r.get("error") == "preflight_over_budget" for r in calls),
        "checkpoint_replies": {str(t): replies.get(t) for t in SPECIAL},
        "linkedin_chars": len(replies.get(520, "")),
    }


async def send_remaining(channel: str) -> None:
    """Real Slack posts + production handler, with a verified-human event envelope.

    User-token posts are app-authored (bot_id), so Socket Mode's bot filter ignores
    them. This explicitly invoked harness calls the real adapter with the posted
    message's human identity/text/timestamp. It does not test Socket Mode delivery.
    """
    settings = get_settings()
    token = dotenv_values(REPO_ROOT / ".env").get("SLACK_USER_TOKEN")
    if not token or not settings.slack_bot_token:
        raise SystemExit("Both Slack user and bot tokens are required")
    user = AsyncWebClient(token=token)
    bot = AsyncWebClient(token=settings.slack_bot_token.get_secret_value())
    human, agent = await user.auth_test(), await bot.auth_test()
    if human.get("bot_id") or human["team_id"] != agent["team_id"]:
        raise SystemExit("Sender must be a verified human in Hazel's workspace")
    current = messages()
    completed = max((m.turn for m in current), default=0)
    with session() as db:
        receipts = db.exec(select(SlackDelivery)).all()
    if len(current) != completed * 2 or sum(d.delivered for d in receipts) != completed:
        raise SystemExit("Resolve pending generation/delivery before resuming")
    evidence = settings.state_path / "scenarios" / "slack-live-demo"
    evidence.mkdir(parents=True, exist_ok=True)
    turns = plan()
    (evidence / "transport.json").write_text(
        json.dumps(
            {
                "socket_mode_through_turn": completed,
                "remaining_transport": "Slack Web API + explicit production handle_event",
                "synthetic_assistant_replies": False,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    for turn in range(completed + 1, 521):
        posted = await user.chat_postMessage(
            channel=channel,
            text=turns[turn - 1],
            as_user=True,
            unfurl_links=False,
            unfurl_media=False,
        )
        event = {
            "type": "message",
            "channel_type": "im",
            "channel": channel,
            "user": human["user_id"],
            "text": turns[turn - 1],
            "ts": posted["ts"],
        }
        await handle_event(
            event,
            body={"team_id": human["team_id"]},
            bot_user_id=agent["user_id"],
            post=bot.chat_postMessage,
        )
        current = messages()
        if max(m.turn for m in current) != turn:
            raise RuntimeError("Concurrent DM or turn drift; stop without resending")
        answer = next(m.text for m in current if m.turn == turn and m.role == Role.ASSISTANT)
        progress = {"completed": turn, "reply_chars": len(answer), "checkpoint": turn in SPECIAL}
        (evidence / "progress.json").write_text(json.dumps(progress), encoding="utf-8")
        print(json.dumps(progress), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--send", metavar="DM_CHANNEL", help="Explicitly send the remaining turns")
    args = parser.parse_args()
    evidence = get_settings().state_path / "scenarios" / "slack-live-demo"
    evidence.mkdir(parents=True, exist_ok=True)
    if args.send:
        logging.basicConfig(level=logging.ERROR)
        asyncio.run(send_remaining(args.send))
        args.report_only = True
    if not args.report_only:
        (evidence / "plan.json").write_text(
            json.dumps(plan(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("Plan written; send through the Slack UI, then use --report-only.")
        return
    result = report()
    (evidence / "report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
