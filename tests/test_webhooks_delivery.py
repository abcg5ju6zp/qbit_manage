"""Tests for the reliable Webhooks delivery path (modules/webhooks.py)."""

import copy
from datetime import datetime

import pytest
import requests

from modules.webhooks import Webhooks

STATS = {
    "added": 0,
    "deleted": 0,
    "deleted_contents": 0,
    "resumed": 0,
    "rechecked": 0,
    "categorized": 0,
    "tagged": 0,
    "rem_unreg": 0,
    "tagged_tracker_error": 0,
    "untagged_tracker_error": 0,
    "orphaned": 0,
    "tagged_noHL": 0,
    "untagged_noHL": 0,
    "updated_share_limits": 0,
    "cleaned_share_limits": 0,
    "recycle_emptied": 0,
    "orphaned_emptied": 0,
}


class FakeResponse:
    def __init__(self, status_code=200, reason="OK", payload=None):
        self.status_code = status_code
        self.reason = reason
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class FakeNotifiarr:
    def __init__(self, apikey="old-key"):
        self.url = "https://notifiarr.com/api/v1/"
        self.apikey = apikey
        self.instance = False


class FakeApprise:
    def __init__(self):
        self.api_url = "http://apprise:8000"
        self.notify_url = "discord://token"


class RecoverySession:
    """Stand-in requests.Session used by the post-restart recovery path."""

    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        return FakeResponse(200)

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        return FakeResponse(200)


class HookConfig:
    """Minimal Config surface the Webhooks class touches."""

    def __init__(self, tmp_path, args=None, commands=None, data=None):
        self.default_dir = str(tmp_path)
        self.args = args if args is not None else {"run_id": "run-A", "validation_mode": False}
        self.commands = commands if commands is not None else {"dry_run": False}
        self.data = data if data is not None else {"qbt": {"host": "qb:8080"}}
        # responder maps: url -> response or Exception
        self.post_responses = {}
        self.get_responses = {}
        self.post_calls = []
        self.get_calls = []
        self.notifications = []
        self.session = RecoverySession()

    def post(self, url, json=None, **kwargs):
        self.post_calls.append((url, copy.deepcopy(json), kwargs))
        responder = self.post_responses.get(url, FakeResponse(200))
        if isinstance(responder, Exception):
            raise responder
        return responder

    def get(self, url, json=None, headers=None, params=None, **kwargs):
        self.get_calls.append((url, copy.deepcopy(json), headers, params))
        responder = self.get_responses.get(url, FakeResponse(200))
        if isinstance(responder, Exception):
            raise responder
        return responder

    def send_notifications(self, attr):
        self.notifications.append(copy.deepcopy(attr))


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    import modules.webhooks as webhooks_mod

    monkeypatch.setattr(webhooks_mod.time, "sleep", lambda *_a, **_k: None)


def make_webhooks(cfg, system_webhooks=None, **kwargs):
    return Webhooks(
        cfg,
        system_webhooks if system_webhooks is not None else {"run_end": ["https://a/"]},
        notifiarr=kwargs.get("notifiarr"),
        apprise=kwargs.get("apprise"),
        web_api_used=kwargs.get("web_api_used", False),
        run_id=kwargs.get("run_id"),
    )


def test_end_hook_content_is_unchanged_and_confirmed(tmp_path):
    cfg = HookConfig(tmp_path)
    wh = make_webhooks(cfg)
    start = datetime(2026, 10, 12, 10, 0, 0)
    end = datetime(2026, 10, 12, 10, 5, 0)
    wh.end_time_hooks(start, end, "0:05:00", None, STATS, "Finished Run")

    assert cfg.post_calls[0][0] == "https://a/"
    assert cfg.post_calls[0][1] == {
        "function": "run_end",
        "title": None,
        "body": "Finished Run",
        "start_time": "2026-10-12 10:00:00",
        "end_time": "2026-10-12 10:05:00",
        "next_run": None,
        "run_time": "0:05:00",
        "torrents_added": 0,
        "torrents_deleted": 0,
        "torrents_deleted_and_contents_count": 0,
        "torrents_resumed": 0,
        "torrents_rechecked": 0,
        "torrents_categorized": 0,
        "torrents_tagged": 0,
        "remove_unregistered": 0,
        "torrents_tagged_tracker_error": 0,
        "torrents_untagged_tracker_error": 0,
        "orphaned_files_found": 0,
        "torrents_tagged_no_hardlinks": 0,
        "torrents_untagged_no_hardlinks": 0,
        "torrents_updated_share_limits": 0,
        "torrents_cleaned_share_limits": 0,
        "files_deleted_from_recyclebin": 0,
        "files_deleted_from_orphaned": 0,
    }
    [delivery] = wh._store.list_deliveries()
    assert delivery["status"] == "confirmed"
    assert delivery["run_id"] == "run-A"
    assert delivery["event_type"] == "run_end"


def test_permanent_rejection_recorded_without_blocking_other_target(tmp_path):
    cfg = HookConfig(tmp_path)
    cfg.post_responses = {"https://bad/": FakeResponse(404, "Not Found"), "https://good/": FakeResponse(200)}
    wh = make_webhooks(cfg, {"run_end": ["https://bad/", "https://good/"]})
    wh.end_time_hooks(datetime.now(), datetime.now(), "0:01", None, STATS, "body")

    statuses = {d["target"]: d["status"] for d in wh._store.list_deliveries()}
    assert set(statuses.values()) == {"confirmed", "failed"}
    failed = wh._store.list_deliveries(status="failed")
    assert failed[0]["last_error"] == "404 [Not Found]"
    # Recovery only retries the unconfirmed target; the good one is never resent.
    cfg.post_calls.clear()
    wh.recover_pending()
    assert cfg.post_calls == []


def test_temporary_5xx_retried_then_confirmed_in_same_send(tmp_path):
    cfg = HookConfig(tmp_path)
    cfg.post_responses = {}  # drive via queue instead
    queue = [FakeResponse(503, "Service Unavailable"), FakeResponse(503, "Service Unavailable"), FakeResponse(200)]

    def post(url, json=None, **kwargs):
        cfg.post_calls.append((url, copy.deepcopy(json), kwargs))
        return queue.pop(0)

    cfg.post = post
    wh = make_webhooks(cfg)
    wh.end_time_hooks(datetime.now(), datetime.now(), "0:01", None, STATS, "body")
    assert len(cfg.post_calls) == 3
    assert wh._store.list_deliveries(status="confirmed")


def test_timeout_stays_pending_and_retry_after_restart_uses_frozen_old_url(tmp_path):
    cfg1 = HookConfig(tmp_path)
    cfg1.post_responses = {"https://old/": requests.exceptions.ConnectionError("https://old/reset")}
    wh1 = make_webhooks(cfg1, {"run_end": ["https://old/"]}, run_id="run-A")
    wh1.end_time_hooks(datetime.now(), datetime.now(), "0:01", None, STATS, "body")
    pending = wh1._store.list_deliveries(status="pending")
    assert len(pending) == 1
    # Simulate time passing past the backoff window.
    wh1._store.conn.execute("UPDATE notification_outbox SET next_attempt_at = 0")
    wh1._store.conn.commit()
    wh1._store.close()

    # Process restart with a CHANGED target configuration: the old event must
    # still go to the frozen old URL, the new URL is never used for it.
    cfg2 = HookConfig(tmp_path, args={"run_id": "run-B", "validation_mode": False})
    wh2 = make_webhooks(cfg2, {"run_end": ["https://new/"]}, run_id="run-B")
    wh2.recover_pending()
    assert [call[1] for call in cfg2.session.calls] == ["https://old/"]
    assert wh2._store.list_deliveries(status="confirmed")
    # A second drain never resends a confirmed target.
    wh2.recover_pending()
    assert len(cfg2.session.calls) == 1


def test_rotated_notifiarr_key_does_not_rewrite_pending_event(tmp_path):
    cfg1 = HookConfig(tmp_path)
    old = FakeNotifiarr("old-key")
    cfg1.get_responses = {
        "https://notifiarr.com/api/v1/notification/qbitManage/": requests.exceptions.Timeout("timed out")
    }
    wh1 = make_webhooks(cfg1, {"run_end": ["notifiarr"]}, notifiarr=old, run_id="run-A")
    wh1.end_time_hooks(datetime.now(), datetime.now(), "0:01", None, STATS, "body")
    wh1._store.conn.execute("UPDATE notification_outbox SET next_attempt_at = 0")
    wh1._store.conn.commit()
    wh1._store.close()

    cfg2 = HookConfig(tmp_path, args={"run_id": "run-B", "validation_mode": False})
    new = FakeNotifiarr("new-key")
    wh2 = make_webhooks(cfg2, {"run_end": ["notifiarr"]}, notifiarr=new, run_id="run-B")
    wh2.recover_pending()
    # Recovery transmits through config.session; inspect the captured GET.
    calls = [c for c in cfg2.session.calls if c[0] == "get"]
    assert len(calls) == 1
    assert calls[0][2]["headers"]["X-API-Key"] == "old-key"
    assert wh2._store.list_deliveries(status="confirmed")


def test_disabled_notifications_create_no_state_and_send_nothing(tmp_path):
    cfg = HookConfig(tmp_path)
    wh = make_webhooks(cfg, {})
    wh.error_hooks("boom", function_error="X")
    wh.end_time_hooks(datetime.now(), datetime.now(), "0:01", None, STATS, "body")
    assert cfg.post_calls == []
    assert not (tmp_path / "notification_state.db").exists()


def test_validation_mode_skips_start_notification(tmp_path):
    cfg = HookConfig(tmp_path, args={"run_id": "run-A", "validation_mode": True}, commands={"dry_run": False})
    wh = make_webhooks(cfg, {"run_start": ["https://a/"]})
    wh.start_time_hooks(datetime.now())
    assert cfg.post_calls == []


def test_dry_run_start_body_preserved(tmp_path):
    cfg = HookConfig(tmp_path, commands={"dry_run": True})
    wh = make_webhooks(cfg, {"run_start": ["https://a/"]})
    wh.start_time_hooks(datetime(2026, 10, 12, 9, 0, 0))
    assert cfg.post_calls[0][1]["body"] == "Starting Dry-Run"
    assert cfg.post_calls[0][1]["dry_run"] is True


def test_apprise_urls_field_never_leaks_into_plain_webhook_target(tmp_path):
    cfg = HookConfig(tmp_path)
    wh = make_webhooks(
        cfg, {"run_end": ["https://a/", "apprise"]}, apprise=FakeApprise(), run_id="run-A"
    )
    wh.end_time_hooks(datetime.now(), datetime.now(), "0:01", None, STATS, "body")
    plain = [call for call in cfg.post_calls if call[0] == "https://a/"]
    apprise_calls = [call for call in cfg.post_calls if call[0] == "http://apprise:8000/notify"]
    assert plain and apprise_calls
    assert "urls" not in plain[0][1]
    assert apprise_calls[0][1]["urls"] == "discord://token"
    assert all(d["status"] == "confirmed" for d in wh._store.list_deliveries())


def test_grouped_notifications_behavior_unchanged(tmp_path):
    cfg = HookConfig(tmp_path)
    wh = make_webhooks(
        cfg,
        {"function": {"cat_update": ["https://a/"]}},
        run_id="run-A",
    )

    def attr(i, category):
        return {
            "function": "cat_update",
            "title": "Category Updated",
            "body": f"t{i}",
            "torrents": [[f"hash{i}"]],
            "torrent_category": category,
            "torrent_tag": None,
            "torrent_tracker": None,
            "notifiarr_indexer": None,
        }

    payload = [attr(i, "same") for i in range(11)]
    wh.notify(torrents_updated=list(range(11)), payload=payload, group_by="category")
    assert len(cfg.notifications) == 1
    assert cfg.notifications[0]["torrent_category"] == "same"
    assert len(cfg.notifications[0]["torrents"]) == 11

    cfg.notifications.clear()
    wh.notify(torrents_updated=[1], payload=[attr(0, "c")], group_by="category")
    assert len(cfg.notifications) == 1
    assert cfg.notifications[0]["torrents"] == [["hash0"]]


def test_tampered_payload_is_refused_without_sending(tmp_path):
    cfg = HookConfig(tmp_path)
    wh = make_webhooks(cfg)
    wh.end_time_hooks(datetime.now(), datetime.now(), "0:01", None, STATS, "body")
    wh._store.conn.execute("UPDATE notification_outbox SET status = 'pending', payload_json = ?", ('{"x": 1}',))
    wh._store.conn.execute("UPDATE notification_outbox SET next_attempt_at = 0, lease_expires_at = 0")
    wh._store.conn.commit()
    cfg.post_calls.clear()
    wh.recover_pending()
    assert cfg.post_calls == []
    assert cfg.session.calls == []
    [failed] = wh._store.list_deliveries(status="failed")
    assert "integrity" in failed["last_error"]
