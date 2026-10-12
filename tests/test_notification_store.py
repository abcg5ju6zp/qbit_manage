"""Tests for the persistent notification outbox (modules/notification_store.py)."""

import json
import os
import time

import pytest
import requests

from modules.notification_store import DB_FILENAME
from modules.notification_store import MAX_ATTEMPTS
from modules.notification_store import OUTCOME_CONFIRMED
from modules.notification_store import OUTCOME_PERMANENT
from modules.notification_store import OUTCOME_TEMPORARY
from modules.notification_store import STATUS_CONFIRMED
from modules.notification_store import STATUS_FAILED
from modules.notification_store import STATUS_PENDING
from modules.notification_store import NotificationStore
from modules.notification_store import canonical_payload
from modules.notification_store import deliver_row
from modules.notification_store import payload_matches
from modules.notification_store import target_key_for
from modules.notification_store import target_label


class FakeResponse:
    def __init__(self, status_code=200, reason="OK", payload=None):
        self.status_code = status_code
        self.reason = reason
        self._payload = payload if payload is not None else {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class RecordingSession:
    def __init__(self, responders=None):
        self.calls = []
        self.responders = responders or {}

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        responder = self.responders.get(url, FakeResponse(200))
        if isinstance(responder, Exception):
            raise responder
        return responder

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        responder = self.responders.get(url, FakeResponse(200))
        if isinstance(responder, Exception):
            raise responder
        return responder


@pytest.fixture
def store(tmp_path):
    s = NotificationStore(str(tmp_path / DB_FILENAME))
    yield s
    s.close()


def _targets(*urls):
    return [("webhook", {"url": u}) for u in urls]


def test_add_event_records_one_pending_row_per_target_with_identity(store):
    payload = {"function": "run_end", "body": "Finished Run"}
    event_id, row_ids = store.add_event("run-1", "run_end", _targets("https://a/", "https://b/"), payload)
    assert event_id and len(row_ids) == 2
    rows = store.get_rows(row_ids)
    raw, digest = canonical_payload(payload)
    for row in rows:
        assert row["run_id"] == "run-1"
        assert row["event_type"] == "run_end"
        assert row["status"] == STATUS_PENDING
        assert row["payload_digest"] == digest
        assert row["payload_json"] == raw.decode("utf-8")
        assert row["lease_expires_at"] is not None  # leased for the live send


def test_confirmed_rows_are_never_claimed_again(store):
    _, row_ids = store.add_event("run-1", "run_end", _targets("https://a/"), {"function": "run_end"})
    store.mark_confirmed(row_ids[0], 200)
    store.conn.execute("UPDATE notification_outbox SET lease_expires_at = 0")
    store.conn.commit()
    assert store.claim_due() == []


def test_claim_respects_backoff_and_lease(store):
    _, row_ids = store.add_event("run-1", "run_end", _targets("https://a/"), {"function": "run_end"})
    # Live send failed: lease released, retry scheduled in the future.
    store.mark_temporary(row_ids[0], "500 [Err]", http_status=500)
    assert store.claim_due() == []  # backoff window
    store.conn.execute("UPDATE notification_outbox SET next_attempt_at = 0, lease_expires_at = 0")
    store.conn.commit()
    claimed = store.claim_due()
    assert len(claimed) == 1
    assert claimed[0]["attempts"] == 2  # one from the failed live send, one from the claim


def test_concurrent_drains_each_row_claimed_once(tmp_path):
    db = str(tmp_path / DB_FILENAME)
    s1 = NotificationStore(db)
    s2 = NotificationStore(db)
    _, row_ids = s1.add_event("run-1", "run_end", _targets("https://a/"), {"function": "run_end"})
    s1.conn.execute("UPDATE notification_outbox SET lease_expires_at = 0, next_attempt_at = 0")
    s1.conn.commit()
    first = s1.claim_due()
    second = s2.claim_due()
    assert len(first) == 1 and second == []
    s1.close()
    s2.close()


def test_temporary_failures_eventually_become_queryable_failures(store):
    _, row_ids = store.add_event("run-1", "run_end", _targets("https://a/"), {"function": "run_end"})
    for _ in range(MAX_ATTEMPTS):
        store.mark_temporary(row_ids[0], "boom")
    failed = store.list_deliveries(status=STATUS_FAILED)
    assert len(failed) == 1
    assert "exhausted" in failed[0]["last_error"]
    assert store.claim_due() == []


def test_permanent_failure_is_independent_and_queryable(store):
    _, row_ids = store.add_event("run-1", "run_end", _targets("https://a/", "https://b/"), {"function": "run_end"})
    store.mark_permanent(row_ids[0], "404 [Not Found]", http_status=404)
    store.mark_confirmed(row_ids[1], 200)
    failed = store.list_deliveries(status=STATUS_FAILED)
    assert len(failed) == 1 and failed[0]["attempts"] == 0
    by_run = store.list_deliveries(run_id="run-1")
    assert {d["status"] for d in by_run} == {STATUS_FAILED, STATUS_CONFIRMED}


def test_list_masks_target_identity(store):
    secret_url = "https://hooks.example.com/services/TOKEN/secret?x=1"
    _, row_ids = store.add_event("run-1", "run_end", _targets(secret_url), {"function": "run_end"})
    store.mark_permanent(row_ids[0], "403", http_status=403)
    [item] = store.list_deliveries(status=STATUS_FAILED)
    assert "TOKEN" not in json.dumps(item)
    assert "secret" not in item["target"]
    assert "hooks.example.com" in item["target"]


def test_payload_digest_detects_tampering(store):
    payload = {"function": "run_end", "body": "original"}
    _, row_ids = store.add_event("run-1", "run_end", _targets("https://a/"), payload)
    [row] = store.get_rows(row_ids)
    assert payload_matches(row["payload_json"], row["payload_digest"])
    assert not payload_matches(json.dumps({"function": "run_end", "body": "tampered"}), row["payload_digest"])


def test_database_file_is_owner_only(tmp_path):
    db = str(tmp_path / DB_FILENAME)
    s = NotificationStore(db)
    s.close()
    mode = os.stat(db).st_mode & 0o777
    assert mode == 0o600


def test_deliver_200_confirms_webhook(tmp_path):
    db = str(tmp_path / DB_FILENAME)
    store = NotificationStore(db)
    _, ids = store.add_event("run-1", "run_end", _targets("https://a/hook"), {"function": "run_end", "body": "x"})
    row = store.get_rows(ids)[0]
    session = RecordingSession()
    outcome, status_code, error = deliver_row(row, session)
    assert outcome == OUTCOME_CONFIRMED and status_code == 200
    assert session.calls[0][0] == "post"
    assert session.calls[0][2]["json"] == {"function": "run_end", "body": "x"}
    store.close()


def test_deliver_classifies_statuses(tmp_path):
    db = str(tmp_path / DB_FILENAME)
    store = NotificationStore(db)
    payload = {"function": "run_end"}
    session_500 = RecordingSession({"https://a/": FakeResponse(503, "Service Unavailable")})
    session_404 = RecordingSession({"https://a/": FakeResponse(404, "Not Found")})
    session_timeout = RecordingSession({"https://a/": requests.exceptions.ConnectionError("boom url with token")})
    for session, expected in (
        (session_500, OUTCOME_TEMPORARY),
        (session_404, OUTCOME_PERMANENT),
        (session_timeout, OUTCOME_TEMPORARY),
    ):
        _, ids = store.add_event("run-1", "run_end", _targets("https://a/"), payload)
        row = store.get_rows(ids)[0]
        outcome, _, error = deliver_row(row, session)
        assert outcome == expected
        assert "token" not in (error or "")
    store.close()


def test_deliver_notifiarr_error_is_permanent_and_preserves_frozen_key(tmp_path):
    db = str(tmp_path / DB_FILENAME)
    store = NotificationStore(db)
    targets = [
        (
            "notifiarr",
            {"url": "https://notifiarr.com/api/v1/", "apikey": "old-key", "instance": False, "qbt_host": "qb:8080"},
        )
    ]
    _, ids = store.add_event("run-1", "run_error", targets, {"function": "run_error", "body": "x"})
    row = store.get_rows(ids)[0]
    session = RecordingSession(
        {
            "https://notifiarr.com/api/v1/notification/qbitManage/": FakeResponse(
                200, payload={"result": "error", "details": {"response": "bad key"}}
            )
        }
    )
    outcome, _, error = deliver_row(row, session)
    assert outcome == OUTCOME_PERMANENT and "bad key" in error
    assert session.calls[0][2]["headers"]["X-API-Key"] == "old-key"
    assert session.calls[0][2]["params"] == {"qbit_client": "qb:8080", "instance": False}
    store.close()


def test_deliver_apprise_injects_urls_only_into_request(tmp_path):
    db = str(tmp_path / DB_FILENAME)
    store = NotificationStore(db)
    targets = [("apprise", {"api_url": "http://apprise:8000", "notify_url": "discord://token"})]
    _, ids = store.add_event("run-1", "run_end", targets, {"function": "run_end", "body": "x"})
    row = store.get_rows(ids)[0]
    session = RecordingSession({"http://apprise:8000/notify": FakeResponse(200)})
    outcome, _, _ = deliver_row(row, session)
    assert outcome == OUTCOME_CONFIRMED
    assert session.calls[0][0] == "post"
    assert session.calls[0][2]["json"]["urls"] == "discord://token"
    # The stored payload itself stays free of the injected urls field.
    assert "urls" not in json.loads(row["payload_json"])
    store.close()


def test_prune_remotes_only_old_history(store):
    _, ids = store.add_event("run-1", "run_end", _targets("https://a/", "https://b/"), {"function": "run_end"})
    store.mark_confirmed(ids[0])
    store.mark_permanent(ids[1], "404")
    old = time.time() - 40 * 24 * 3600
    store.conn.execute(
        "UPDATE notification_outbox SET confirmed_at = ?, updated_at = ? WHERE status = ?",
        (old, old, STATUS_CONFIRMED),
    )
    store.conn.execute("UPDATE notification_outbox SET updated_at = ? WHERE status = ?", (old, STATUS_FAILED))
    store.conn.commit()
    store.prune()
    assert store.list_deliveries() == []


def test_target_key_and_label_never_expose_url():
    key = target_key_for("webhook", {"url": "https://h.example.com/t/abcdef"})
    label = target_label("webhook", key, json.dumps({"url": "https://h.example.com/t/abcdef"}))
    assert key.startswith("webhook:")
    assert "abcdef" not in label
    assert "h.example.com" in label
