"""Slack mention filtering, delivery recovery, ordering and core memory boundaries."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

from chatbot import core
from chatbot.config import get_settings
from chatbot.memory import messages, records, refs
from chatbot.memory.db import session
from chatbot.memory.forget import forget_values
from chatbot.memory.models import SlackDelivery, ThreadKind
from chatbot.memory.records import MemoryCaller
from chatbot.surfaces import slack_app as slack

BOT = "UHAZEL"
BASE = {
    "type": "app_mention",
    "user": "U1",
    "channel": "CONE",
    "ts": "1.001",
    "text": "<@UHAZEL> hi",
}


def invoke(event=BASE, post=None):
    return slack.handle_event(
        event, body={"team_id": "T1"}, bot_user_id=BOT, post=post or AsyncMock()
    )


@pytest.mark.parametrize(
    "extra",
    [
        {"bot_id": "B1"},
        {"user": BOT},
        {"user": ""},
        {"hidden": True},
        {"subtype": "message_changed"},
        {"subtype": "file_share"},
        {"type": "message", "channel_type": "channel"},
        {"channel_type": "mpim"},
        {"channel_type": "im"},
        {"text": "hi @Hazel"},
        {"text": "<@UOTHER> hi"},
        {"text": "<@UHAZEL>  "},
    ],
)
def test_filters(extra):
    assert not slack.should_handle({**BASE, **extra}, None, BOT)


@pytest.mark.parametrize("is_dm", [False, True])
def test_receipt_precedes_generation(is_dm, monkeypatch):
    react = AsyncMock()
    real = slack.send_async

    async def generate(*args):
        react.assert_awaited_once_with(channel=event["channel"], timestamp=event["ts"], name="eyes")
        return await real(*args)

    event = dict(BASE)
    if is_dm:
        event.update(type="message", channel_type="im", channel="D1")
    monkeypatch.setattr(slack, "send_async", generate)
    asyncio.run(slack.handle_event(event, body={}, bot_user_id=BOT, post=AsyncMock(), react=react))


def test_receipt_failure_does_not_block_and_ignored_events_do_not_react():
    async def check():
        react = AsyncMock(side_effect=RuntimeError("missing_scope"))
        post = AsyncMock()
        await slack.handle_event(BASE, body={}, bot_user_id=BOT, post=post, react=react)
        post.assert_awaited_once()
        react.reset_mock()
        await slack.handle_event(
            {**BASE, "bot_id": "B1"}, body={}, bot_user_id=BOT, post=post, react=react
        )
        react.assert_not_awaited()

    asyncio.run(check())


def test_normalizes_only_hazel_and_checks_allowed_user():
    assert (
        slack.normalize_text("<@UHAZEL|Hazel> ask <@U2> <https://example.com|site> <@UHAZEL>", BOT)
        == "ask <@U2> <https://example.com|site>"
    )
    assert slack.should_handle(BASE, "U1", BOT)
    assert not slack.should_handle(BASE, "U2", BOT)


def test_routing_and_durable_dedup():
    async def check():
        post = AsyncMock()
        await invoke(post=post)
        slack._locks.clear()  # restart loses locks, not receipts
        await invoke({**BASE, "event_ts": "new-wrapper"}, post)
        assert post.await_count == 1
        assert post.call_args.kwargs["thread_ts"] == BASE["ts"]
        await invoke({**BASE, "ts": "2.001", "thread_ts": "0.001"}, post)
        assert post.call_args.kwargs["thread_ts"] == "0.001"
        await invoke(
            {
                **BASE,
                "type": "message",
                "channel_type": "im",
                "channel": "D1",
                "ts": "3.001",
                "text": "hi",
            },
            post,
        )
        assert post.call_args.kwargs["thread_ts"] is None
        await invoke(
            {
                **BASE,
                "type": "message",
                "channel_type": "im",
                "channel": "D1",
                "ts": "4.001",
                "thread_ts": "3.001",
            },
            post,
        )
        assert post.call_args.kwargs["thread_ts"] == "3.001"

    asyncio.run(check())
    assert len(messages.thread_messages("slack-CONE")) == 4
    assert len(messages.thread_messages("slack-dm")) == 4
    assert (
        messages.ensure_thread(get_settings().default_user_id, "slack-CONE").kind
        == ThreadKind.SLACK
    )


def test_failed_generation_is_retryable(monkeypatch):
    real = slack.send_async
    fake = AsyncMock(side_effect=RuntimeError("offline"))
    monkeypatch.setattr(slack, "send_async", fake)
    with pytest.raises(RuntimeError):
        asyncio.run(invoke())
    with session() as s:
        assert s.exec(select(SlackDelivery)).all() == []
    monkeypatch.setattr(slack, "send_async", real)
    asyncio.run(invoke())
    assert len(messages.thread_messages("slack-CONE")) == 2


def test_post_failure_reuses_core_reply_after_restart(monkeypatch):
    post = AsyncMock(side_effect=RuntimeError("Slack down"))
    with pytest.raises(RuntimeError):
        asyncio.run(invoke(post=post))
    monkeypatch.setattr(
        slack, "send_async", AsyncMock(side_effect=AssertionError("must not regenerate"))
    )
    slack._locks.clear()
    post = AsyncMock()
    asyncio.run(invoke(post=post))
    assert post.call_args.kwargs["text"] == "ok"
    assert len(messages.thread_messages("slack-CONE")) == 2


def test_forget_deletes_pending_output(monkeypatch):
    async def generate(user, tid, text):
        messages.ensure_thread(user, tid)
        messages.record_turn(user, tid, text, "secret-marigold")
        return "secret-marigold"

    monkeypatch.setattr(slack, "send_async", generate)
    with pytest.raises(RuntimeError):
        asyncio.run(invoke(post=AsyncMock(side_effect=RuntimeError())))
    forget_values(get_settings().default_user_id, ["secret-marigold"])
    post = AsyncMock()
    asyncio.run(invoke(post=post))
    post.assert_not_awaited()


def test_serializes_complete_turns_but_allows_other_channels(monkeypatch):
    real = slack.send_async
    entered, delivered = [], []

    async def generate(user, tid, text):
        entered.append(text)
        if text == "slow":
            await asyncio.sleep(0.03)
        return await real(user, tid, text)

    async def post(**kw):
        delivered.append(kw["thread_ts"])

    monkeypatch.setattr(slack, "send_async", generate)

    async def check():
        await asyncio.gather(
            invoke({**BASE, "text": "<@UHAZEL> slow"}, post),
            invoke({**BASE, "text": "<@UHAZEL> fast", "ts": "2"}, post),
            invoke({**BASE, "text": "<@UHAZEL> other", "channel": "CTWO", "ts": "3"}, post),
            invoke(post=post),  # same message while first invocation is in flight
        )

    asyncio.run(check())
    assert entered == ["slow", "other", "fast"]
    assert delivered == ["3", "1.001", "2"]


def test_shared_records_and_local_sources_are_scoped_across_real_channel_turns():
    # Every destination enters core; ignoring channel events cannot pass this check.
    for channel in ("CONE", "CTWO"):
        asyncio.run(invoke({**BASE, "channel": channel}))
    user = get_settings().default_user_id
    a = MemoryCaller(user, "slack-CONE")
    source = refs.msg(messages.thread_messages(a.session_id)[0].id)
    shared = records.write(
        a,
        store="long_term",
        kind="fact",
        key="coordinator",
        text="Coordinator is Cobalt Finch",
        sources=[source],
    )
    local = records.write(
        a,
        store="session",
        kind="task",
        key="draft",
        text="Use LOCAL-MARK for this draft only",
        sources=[source],
    )
    for tid in ("slack-CTWO", "slack-dm", "main-existing"):
        core.send(user, tid, "Write a plain announcement")
        caller = MemoryCaller(user, tid)
        assert records.read(caller, shared.ref) is not None
        with pytest.raises(ValueError):
            records.read(caller, local.ref)
        with pytest.raises(ValueError):
            records.read(caller, source)
        assert "LOCAL-MARK" not in messages.thread_messages(tid)[-1].text


def test_slack_format_preserves_code_and_converts_common_markdown():
    result = slack.slack_format(
        "# Heading\n**Bold** [Site](https://example.com)\n"
        "| A | B |\n| --- | --- |\n| 1 | 2 |\n`**code**`\n```\n**raw**\n```\n<@U2>"
    )
    assert "*Heading*\n*Bold* <https://example.com|Site>" in result
    assert "A • B\n1 • 2" in result and "---" not in result
    assert "`**code**`" in result and "```\n**raw**\n```" in result
    assert "&lt;@U2&gt;" in result  # model output cannot trigger unsolicited mentions


def test_later_event_cannot_overtake_delivery_retry():
    posted, attempts = [], 0

    async def post(**kw):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary post failure")
        posted.append(kw["thread_ts"])

    async def check():
        await asyncio.gather(
            slack.handle_event(
                BASE, body={"team_id": "T1"}, bot_user_id=BOT, post=post, attempts=2
            ),
            invoke({**BASE, "ts": "2.001"}, post),
        )

    asyncio.run(check())
    assert posted == ["1.001", "2.001"]
    assert len(messages.thread_messages("slack-CONE")) == 4  # no regenerated turn
