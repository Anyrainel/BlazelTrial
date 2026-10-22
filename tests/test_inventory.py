"""Human inventory ownership, pagination and content coverage."""

from fastapi.testclient import TestClient

from chatbot import core
from chatbot.config import get_settings
from chatbot.memory.db import session
from chatbot.memory.models import MemoryRecord, OutboundMessage, RecordKind, Store
from chatbot.surfaces.http import app


def test_inventory_filters_owners_and_includes_versions_and_mock_messages():
    core.ingest("me", "A", "My colleague is Xi", "ok")
    core.ingest("someone-else", "B", "Private", "ok")
    with session() as s:
        for owner, key, text in (("me", "colleague", "Xi"), ("someone-else", "secret", "hidden")):
            s.add(
                MemoryRecord(
                    user_id=owner, store=Store.LONG_TERM, kind=RecordKind.FACT, key=key, text=text
                )
            )
        s.add(OutboundMessage(thread_id="A", recipient="team", text="Visible draft"))
        s.add(OutboundMessage(thread_id="B", recipient="private", text="Hidden draft"))
        s.commit()
    c = TestClient(app)
    log = get_settings().llm_log_path
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        '{"thread_id":"A","purpose":"chat","input_tokens":20,"api_key":"hidden-key"}\n'
        '{"thread_id":"B","purpose":"private","input_tokens":30}\n'
        "incomplete line",
        encoding="utf-8",
    )
    inventory = c.get("/agent/inventory")
    assert inventory.status_code == 200
    assert {r["id"]: r["count"] for r in inventory.json()["categories"]}["records"] == 1
    assert inventory.json()["capabilities"]
    rows = c.get("/agent/inventory/records?q=Xi&limit=1").json()
    assert rows["total"] == 1 and rows["items"][0]["text"] == "Xi"
    assert "idempotency_key" not in rows["items"][0]
    assert c.get("/agent/inventory/records?q=hidden").json()["total"] == 0
    assert c.get("/agent/inventory/outbound").json()["total"] == 1
    assert c.get("/agent/inventory/records?offset=1").json()["items"] == []
    calls = c.get("/agent/inventory/model_calls")
    assert calls.json()["total"] == 1
    assert "hidden-key" not in calls.text and "private" not in calls.text
    assert c.get("/agent/inventory/unknown").status_code == 404
    assert c.get("/agent/inventory/records?limit=10000").status_code == 422
    for category in inventory.json()["categories"]:
        assert c.get(f"/agent/inventory/{category['id']}").status_code == 200


def test_status_exposes_delivery_limits_without_credentials():
    response = TestClient(app).get("/agent/status")
    assert response.status_code == 200
    body = response.json()
    assert body["count_required_before_inference"] is True
    assert any("No background drain" in item for item in body["limitations"])
    assert "test-key" not in response.text
