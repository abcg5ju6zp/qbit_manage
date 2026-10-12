"""Persistent outbox for webhook / Notifiarr / Apprise notifications.

One row represents a single ``(run, event, target)`` delivery. Rows are
written with the fully resolved payload and target settings *before* the
HTTP request is made and are only flipped to ``confirmed`` once the target
acknowledges. A process killed after the business result was produced, a
timed-out request, a target removed or reconfigured in config.yml, or two
runs executing concurrently therefore can neither lose the final result
nor deliver a duplicate event to a target that already confirmed it.

Frozen target settings (webhook URL, Notifiarr API key, Apprise API/notify
URLs) are captured per event: pending rows are always retried with the
settings they were created with, never rewritten from a newer — possibly
rotated — configuration. The database lives next to config.yml and holds
the same kinds of secrets, so it is created owner-only.
"""

import hashlib
import json
import os
import sqlite3
import time
import uuid
from json import JSONDecodeError
from urllib.parse import urlparse

import requests

from modules import util

logger = util.logger

DB_FILENAME = "notification_state.db"

STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_FAILED = "failed"

OUTCOME_CONFIRMED = "confirmed"
OUTCOME_TEMPORARY = "temporary"
OUTCOME_PERMANENT = "permanent"

# Cross-process retry schedule after a live attempt could not be confirmed.
BACKOFF_SECONDS = [30, 60, 300, 900, 1800, 3600, 3 * 3600, 6 * 3600, 12 * 3600]
MAX_BACKOFF_SECONDS = 24 * 3600
MAX_ATTEMPTS = 100
MAX_AGE_SECONDS = 7 * 24 * 3600
# Leased rows are invisible to other drains; a crashed sender's lease
# expires so the row becomes retryable.
LEASE_SECONDS = 120
CONFIRMED_RETENTION_SECONDS = 7 * 24 * 3600
FAILED_RETENTION_SECONDS = 30 * 24 * 3600
DRAIN_LIMIT = 50
RECOVERY_TIMEOUT = (5, 15)


def canonical_payload(payload):
    """Return the canonical JSON serialization and its sha256 digest."""
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return raw, hashlib.sha256(raw).hexdigest()


def payload_matches(payload_json, digest):
    """Verify stored payload bytes still match their recorded digest."""
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError):
        return False
    _, computed = canonical_payload(payload)
    return computed == digest


def target_key_for(kind, spec):
    """Stable non-secret identity of a target within an event."""
    if kind == "webhook":
        return "webhook:" + hashlib.sha256(spec["url"].encode("utf-8")).hexdigest()[:16]
    return kind


def target_label(kind, key, target_json):
    """Human-facing target label that never embeds URLs, keys or tokens."""
    suffix = key.split(":", 1)[1] if ":" in key else ""
    host = ""
    try:
        spec = json.loads(target_json) if isinstance(target_json, str) else target_json
        endpoint = spec.get("url") if kind == "webhook" else spec.get("api_url")
        if endpoint:
            parsed = urlparse(endpoint)
            host = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
    except (TypeError, ValueError):
        host = ""
    return " ".join(part for part in (kind, host, f"#{suffix}" if suffix else "") if part)


def classify_response(kind, response):
    """Classify an HTTP response the same way the legacy ``_request`` did.

    Returns ``(outcome, http_status, error)``. ``error`` never contains the
    request URL or any other secret.
    """
    status_code = getattr(response, "status_code", None)
    result_error = None
    try:
        response_json = response.json()
        if (
            isinstance(response_json, dict)
            and response_json.get("result") == "error"
            and isinstance(response_json.get("details"), dict)
            and "response" in response_json["details"]
        ):
            result_error = str(response_json["details"]["response"])
    except (JSONDecodeError, ValueError, requests.JSONDecodeError):
        response_json = None

    if result_error is not None:
        return OUTCOME_PERMANENT, status_code, f"Notifiarr Error: Unable to send notification ({result_error})"
    if status_code is not None and status_code >= 500:
        return OUTCOME_TEMPORARY, status_code, f"{status_code} [{response.reason}]"
    if status_code is not None and status_code >= 400:
        return OUTCOME_PERMANENT, status_code, f"{status_code} [{response.reason}]"
    logger.trace(f"Response: {response_json}")
    return OUTCOME_CONFIRMED, status_code, None


def build_request_kwargs(kind, spec, payload, timeout=None):
    """Build ``(method, url, kwargs)`` from a frozen target spec."""
    if kind == "webhook":
        url = spec["url"]
        kwargs = {"json": payload}
    elif kind == "apprise":
        url = f"{spec['api_url']}/notify"
        kwargs = {"json": {**payload, "urls": spec["notify_url"]}}
    else:
        url = f"{spec['url']}notification/qbitManage/"
        kwargs = {
            "json": payload,
            "headers": {"X-API-Key": spec["apikey"]},
            "params": {"qbit_client": spec.get("qbt_host"), "instance": spec.get("instance")},
        }
    if timeout is not None:
        kwargs["timeout"] = timeout
    return ("get" if kind == "notifiarr" else "post"), url, kwargs


def deliver_row(row, session, timeout=None):
    """Send one claimed outbox row using its frozen target settings.

    Returns ``(outcome, http_status, error)``. Used on the recovery path;
    the live path reuses this with the config's retrying session helpers.
    """
    spec = json.loads(row["target_json"])
    payload = json.loads(row["payload_json"])
    method, url, kwargs = build_request_kwargs(kind=row["target_kind"], spec=spec, payload=payload, timeout=timeout)
    try:
        response = getattr(session, method)(url, **kwargs)
    except requests.exceptions.RequestException as exc:
        return OUTCOME_TEMPORARY, None, type(exc).__name__
    except Exception as exc:  # defensive: one target must not block the rest
        return OUTCOME_TEMPORARY, None, type(exc).__name__
    return classify_response(row["target_kind"], response)


class NotificationStore:
    """SQLite-backed per-target notification outbox."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS notification_outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        event_function TEXT,
        target_kind TEXT NOT NULL,
        target_key TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        payload_digest TEXT NOT NULL,
        target_json TEXT NOT NULL,
        status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        http_status INTEGER,
        last_error TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        next_attempt_at REAL NOT NULL,
        lease_expires_at REAL,
        lease_token TEXT,
        confirmed_at REAL,
        UNIQUE(event_id, target_key)
    );
    CREATE INDEX IF NOT EXISTS idx_outbox_due ON notification_outbox(status, next_attempt_at);
    CREATE INDEX IF NOT EXISTS idx_outbox_run ON notification_outbox(run_id);
    """

    def __init__(self, db_path, create=True):
        self.db_path = db_path
        if not create and not os.path.exists(db_path):
            self.conn = None
            return
        if db_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.conn = sqlite3.connect(db_path, timeout=15)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(self.SCHEMA)
        self.conn.commit()
        try:
            os.chmod(db_path, 0o600)
            for suffix in ("-wal", "-shm"):
                sidecar = db_path + suffix
                if os.path.exists(sidecar):
                    os.chmod(sidecar, 0o600)
        except OSError:
            pass

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    @staticmethod
    def _backoff_seconds(attempts):
        if attempts <= 0:
            return 0
        if attempts <= len(BACKOFF_SECONDS):
            return BACKOFF_SECONDS[attempts - 1]
        return MAX_BACKOFF_SECONDS

    def _exhausted(self, attempts, created_at, now):
        return attempts >= MAX_ATTEMPTS or (now - created_at) >= MAX_AGE_SECONDS

    def add_event(self, run_id, event_type, targets, payload, event_function=None):
        """Persist one pending row per target. Returns ``(event_id, row_ids)``.

        ``targets`` is a list of ``(kind, spec)`` tuples. Rows are leased to
        this writer for the immediate live send so a concurrent drain cannot
        transmit the same event in parallel.
        """
        if self.conn is None or not targets:
            return None, []
        event_id = uuid.uuid4().hex
        raw, digest = canonical_payload(payload)
        now = time.time()
        lease_token = uuid.uuid4().hex
        row_ids = []
        with self.conn:
            for kind, spec in targets:
                key = target_key_for(kind, spec)
                cur = self.conn.execute(
                    """
                    INSERT OR IGNORE INTO notification_outbox
                        (event_id, run_id, event_type, event_function, target_kind, target_key,
                         payload_json, payload_digest, target_json, status, attempts,
                         created_at, updated_at, next_attempt_at, lease_expires_at, lease_token)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        run_id,
                        event_type,
                        event_function,
                        kind,
                        key,
                        raw.decode("utf-8"),
                        digest,
                        json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
                        STATUS_PENDING,
                        0,
                        now,
                        now,
                        now,
                        now + LEASE_SECONDS,
                        lease_token,
                    ),
                )
                if cur.lastrowid:
                    row_ids.append(cur.lastrowid)
        return event_id, row_ids

    def get_rows(self, row_ids):
        """Fetch rows by id (live send path)."""
        if not row_ids or self.conn is None:
            return []
        placeholders = ",".join("?" for _ in row_ids)
        return list(self.conn.execute(f"SELECT * FROM notification_outbox WHERE id IN ({placeholders})", row_ids))

    def renew_lease(self, row_id, token=None):
        """Extend the live-send lease before an HTTP attempt."""
        if self.conn is None:
            return
        with self.conn:
            self.conn.execute(
                "UPDATE notification_outbox SET lease_expires_at = ?, lease_token = COALESCE(?, lease_token), "
                "updated_at = ? WHERE id = ? AND status = ?",
                (time.time() + LEASE_SECONDS, token, time.time(), row_id, STATUS_PENDING),
            )

    def claim_due(self, limit=DRAIN_LIMIT, now=None):
        """Atomically claim due, lease-free pending rows for this process."""
        if self.conn is None:
            return []
        now = now or time.time()
        token = uuid.uuid4().hex
        with self.conn:
            self.conn.execute(
                """
                UPDATE notification_outbox
                SET attempts = attempts + 1,
                    lease_expires_at = ?,
                    lease_token = ?,
                    updated_at = ?
                WHERE id IN (
                    SELECT id FROM notification_outbox
                    WHERE status = ?
                      AND next_attempt_at <= ?
                      AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                    ORDER BY next_attempt_at, id
                    LIMIT ?
                )
                """,
                (now + LEASE_SECONDS, token, now, STATUS_PENDING, now, now, limit),
            )
            rows = list(
                self.conn.execute(
                    "SELECT * FROM notification_outbox WHERE lease_token = ? AND status = ? ORDER BY id",
                    (token, STATUS_PENDING),
                )
            )
        return rows

    def mark_confirmed(self, row_id, http_status=None, now=None):
        if self.conn is None:
            return
        now = now or time.time()
        with self.conn:
            self.conn.execute(
                "UPDATE notification_outbox SET status = ?, http_status = ?, last_error = NULL, "
                "lease_expires_at = NULL, lease_token = NULL, confirmed_at = ?, updated_at = ? WHERE id = ?",
                (STATUS_CONFIRMED, http_status, now, now, row_id),
            )

    def mark_temporary(self, row_id, error, http_status=None, now=None):
        """Keep unconfirmed and schedule the next retry; eventually give up."""
        if self.conn is None:
            return
        now = now or time.time()
        with self.conn:
            row = self.conn.execute("SELECT attempts, created_at FROM notification_outbox WHERE id = ?", (row_id,)).fetchone()
            attempts = (row["attempts"] if row else 0) + 1
            if row is not None and self._exhausted(attempts, row["created_at"], now):
                status, next_at = STATUS_FAILED, now
                final_error = f"{error} (delivery attempts exhausted)"
            else:
                status, next_at = STATUS_PENDING, now + self._backoff_seconds(attempts)
                final_error = error
            self.conn.execute(
                "UPDATE notification_outbox SET status = ?, attempts = ?, http_status = ?, last_error = ?, "
                "next_attempt_at = ?, lease_expires_at = NULL, lease_token = NULL, updated_at = ? WHERE id = ?",
                (status, attempts, http_status, final_error, next_at, now, row_id),
            )

    def mark_permanent(self, row_id, error, http_status=None, now=None):
        if self.conn is None:
            return
        now = now or time.time()
        with self.conn:
            self.conn.execute(
                "UPDATE notification_outbox SET status = ?, http_status = ?, last_error = ?, "
                "next_attempt_at = ?, lease_expires_at = NULL, lease_token = NULL, updated_at = ? WHERE id = ?",
                (STATUS_FAILED, http_status, error, now, now, row_id),
            )

    def list_deliveries(self, status=None, run_id=None, limit=100):
        """Return deliveries (oldest first) for the query API.

        Targets are rendered as non-secret labels; no payloads or target
        settings leave the database through this method.
        """
        if self.conn is None:
            return []
        clauses = []
        params = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 500)))
        rows = self.conn.execute(
            f"""
            SELECT event_id, run_id, event_type, event_function, target_kind, target_key, target_json,
                   status, attempts, http_status, last_error, created_at, updated_at, confirmed_at
            FROM notification_outbox {where} ORDER BY id DESC LIMIT ?
            """,
            params,
        ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "run_id": row["run_id"],
                "event_type": row["event_type"],
                "event_function": row["event_function"],
                "target_kind": row["target_kind"],
                "target": target_label(row["target_kind"], row["target_key"], row["target_json"]),
                "status": row["status"],
                "attempts": row["attempts"],
                "http_status": row["http_status"],
                "last_error": row["last_error"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "confirmed_at": row["confirmed_at"],
            }
            for row in rows
        ]

    def prune(self, now=None):
        """Drop history rows that are no longer needed for dedup or queries."""
        if self.conn is None:
            return
        now = now or time.time()
        with self.conn:
            self.conn.execute(
                "DELETE FROM notification_outbox WHERE status = ? AND confirmed_at IS NOT NULL AND confirmed_at < ?",
                (STATUS_CONFIRMED, now - CONFIRMED_RETENTION_SECONDS),
            )
            self.conn.execute(
                "DELETE FROM notification_outbox WHERE status = ? AND updated_at < ?",
                (STATUS_FAILED, now - FAILED_RETENTION_SECONDS),
            )
