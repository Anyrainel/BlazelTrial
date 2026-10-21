"""HTTP and Slack surfaces are thin adapters over the core."""

import asyncio

from fastapi.testclient import TestClient

from chatbot.agent.agent import get_agent
from chatbot.agent.models import build_model
from chatbot.config import get_settings
from chatbot.memory import messages as store
from chatbot.surfaces import slack_app
from tests.fakes import scripted


def client() -> TestClient:
    from chatbot.surfaces.http import app

    return TestClient(app)


def test_http_send_goes_through_the_core():
    with get_agent().override(
        model=build_model(get_settings(), raw=scripted([], "hello from core"))
    ):
        r = client().post("/send", json={"thread_id": "s-1", "text": "hi"})
    assert r.status_code == 200 and r.json() == {"reply": "hello from core", "thread_id": "s-1"}
    assert [m.text for m in store.thread_messages("s-1")] == ["hi", "hello from core"]


def test_http_threads_and_history_for_the_web_ui():
    c = client()
    new = c.post("/threads").json()
    assert new["id"].startswith("s-")
    c.post("/send", json={"thread_id": new["id"], "text": "Plan the offsite agenda"})
    c.post("/send", json={"thread_id": "slack-dm", "text": "hey"})
    listed = {t["id"]: t for t in c.get("/threads").json()}
    assert listed[new["id"]]["title"] == "Plan the offsite agenda"
    assert listed["slack-dm"]["title"] == "Slack DM"
    history = c.get(f"/threads/{new['id']}/messages").json()
    assert [m["role"] for m in history] == ["user", "assistant"]


def test_http_rejects_empty_messages():
    assert client().post("/send", json={"thread_id": "s-1", "text": ""}).status_code == 422


def test_slack_dms_map_to_the_slack_thread_and_the_single_user():
    event = {"channel_type": "im", "user": "U1", "text": "Who's my manager?"}
    with get_agent().override(model=build_model(get_settings(), raw=scripted([], "Priya"))):
        reply = asyncio.run(slack_app.handle_dm(event, event_id="Ev1"))
    assert reply == "Priya"
    [user_msg, _] = store.thread_messages("slack-dm")
    assert user_msg.user_id == get_settings().default_user_id


def test_slack_ignores_bots_edits_other_channels_and_redeliveries(monkeypatch):
    base = {"channel_type": "im", "user": "U1", "text": "hi"}
    assert not slack_app.should_handle({**base, "bot_id": "B1"}, None)
    assert not slack_app.should_handle({**base, "subtype": "message_changed"}, None)
    assert not slack_app.should_handle({**base, "channel_type": "channel"}, None)
    assert not slack_app.should_handle({**base, "text": "  "}, None)
    assert not slack_app.should_handle(base, "U2")  # SLACK_ALLOWED_USER set to someone else
    assert slack_app.should_handle(base, "U1")
    assert slack_app.first_time("Ev-dup") and not slack_app.first_time("Ev-dup")
