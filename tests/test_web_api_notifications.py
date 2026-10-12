"""Tests for GET /api/notification-deliveries."""

import asyncio

import pytest
from fastapi import HTTPException

from modules.notification_store import STATUS_FAILED
from modules.notification_store import DB_FILENAME
from modules.notification_store import NotificationStore
from modules.web_api import WebAPI


@pytest.fixture
def api(tmp_path):
    web_api = object.__new__(WebAPI)
    object.__setattr__(web_api, "default_dir", str(tmp_path))
    return web_api


def test_missing_store_returns_empty_without_creating_db(api, tmp_path):
    result = asyncio.run(api.get_notification_deliveries())
    assert result == {"deliveries": []}
    assert not (tmp_path / DB_FILENAME).exists()


def test_failed_deliveries_are_listed_without_secrets(api, tmp_path):
    store = NotificationStore(str(tmp_path / DB_FILENAME))
    secret_url = "https://hooks.example.com/services/SUPERSECRET"
    store.add_event("run-1", "run_end", [("webhook", {"url": secret_url})], {"function": "run_end"})
    rows = store.get_rows([store.conn.execute("SELECT id FROM notification_outbox").fetchone()[0]])
    store.mark_permanent(rows[0]["id"], "404 [Not Found]", http_status=404)
    store.close()

    result = asyncio.run(api.get_notification_deliveries(status=STATUS_FAILED))
    [delivery] = result["deliveries"]
    assert delivery["status"] == STATUS_FAILED
    assert delivery["run_id"] == "run-1"
    assert delivery["event_type"] == "run_end"
    assert "SUPERSECRET" not in str(delivery)

    result = asyncio.run(api.get_notification_deliveries(status="confirmed"))
    assert result["deliveries"] == []


def test_invalid_status_rejected(api):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.get_notification_deliveries(status="bogus"))
    assert exc.value.status_code == 400
