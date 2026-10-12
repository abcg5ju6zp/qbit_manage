"""Data model for persisted execution plans."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Any
from uuid import uuid4

SCHEMA_VERSION = 1

# Retention for plan artifacts under <config_dir>/plans/.
PLAN_RETENTION_DAYS = 30


class ActionResult:
    PENDING = "pending"
    APPLIED = "applied"
    SKIPPED = "skipped"
    STALE = "stale"
    FAILED = "failed"
    UNCERTAIN = "uncertain"

    TERMINAL = {APPLIED, SKIPPED, STALE, FAILED, UNCERTAIN}
    DEFINITE = {APPLIED, SKIPPED, STALE}  # outcomes allowed in summaries/notifications


class PlanStatus:
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_FAILURES = "completed_with_failures"
    INVALID = "invalid"

    # A plan left in RUNNING by a crashed process may be resumed; only INVALID
    # plans can never be confirmed.
    CONFIRMABLE = {PENDING, RUNNING, COMPLETED_WITH_FAILURES}


class ActionType:
    # tags command
    TAGS_ADD = "tags_add"
    TAGS_REMOVE = "tags_remove"
    # category command
    CATEGORY_SET = "category_set"
    CATEGORY_ENABLE_TMM = "category_enable_tmm"
    # recheck command
    RESUME = "resume"
    RECHECK = "recheck"
    # tag_nohardlinks command
    NOHL_ADD = "nohl_add"
    NOHL_REMOVE = "nohl_remove"
    # remove_unregistered / tag_tracker_error commands
    TRACKER_ERROR_ADD = "tracker_error_add"
    TRACKER_ERROR_REMOVE = "tracker_error_remove"
    UNREG_DELETE = "unreg_delete"
    # share_limits command
    SHARE_TAG_ADD = "share_tag_add"
    SHARE_TAG_REMOVE = "share_tag_remove"
    SET_SHARE_LIMITS = "set_share_limits"
    SET_UPLOAD_LIMIT = "set_upload_limit"
    SHARE_RESUME = "share_resume"
    SHARE_CLEANUP_DELETE = "share_cleanup_delete"
    # tag definition cleanup (client.torrent_tags.delete_tags)
    TAG_DEFINITION_DELETE = "tag_definition_delete"
    # rem_orphaned command
    ORPHAN_FILE_DELETE = "orphan_file_delete"
    ORPHAN_FILE_MOVE = "orphan_file_move"
    ORPHAN_RMDIR = "orphan_rmdir"


# Torrent snapshot keys each action type is sensitive to. Confirmation fails
# the action (stale) when any watched key differs from the live torrent.
WATCHED_KEYS = {
    ActionType.TAGS_ADD: ["tags"],
    ActionType.TAGS_REMOVE: ["tags"],
    ActionType.CATEGORY_SET: ["category", "auto_tmm", "save_path"],
    ActionType.CATEGORY_ENABLE_TMM: ["category", "auto_tmm"],
    ActionType.RESUME: ["state", "progress", "ratio_limit", "seeding_time_limit"],
    ActionType.RECHECK: ["state", "progress"],
    ActionType.NOHL_ADD: ["tags"],
    ActionType.NOHL_REMOVE: ["tags"],
    ActionType.TRACKER_ERROR_ADD: ["tags", "trackers"],
    ActionType.TRACKER_ERROR_REMOVE: ["tags", "trackers"],
    ActionType.UNREG_DELETE: ["category", "tags", "state", "trackers", "content_path"],
    ActionType.SHARE_TAG_ADD: ["tags"],
    ActionType.SHARE_TAG_REMOVE: ["tags"],
    ActionType.SET_SHARE_LIMITS: ["tags", "ratio_limit", "seeding_time_limit", "inactive_seeding_time_limit", "state"],
    ActionType.SET_UPLOAD_LIMIT: ["tags", "up_limit", "state"],
    ActionType.SHARE_RESUME: ["state"],
    ActionType.SHARE_CLEANUP_DELETE: ["category", "tags", "state", "trackers", "content_path"],
}

# Action types that target a single torrent and require a live hash lookup.
HASH_TYPES = {k for k in WATCHED_KEYS}

# Action types that delete a torrent from qBittorrent.
TORRENT_DELETE_TYPES = {ActionType.UNREG_DELETE, ActionType.SHARE_CLEANUP_DELETE}

# Action types that carry filesystem snapshots that must keep their identity.
FILE_SNAPSHOT_TYPES = TORRENT_DELETE_TYPES | {ActionType.NOHL_ADD, ActionType.NOHL_REMOVE}

ORPHAN_FILE_TYPES = {ActionType.ORPHAN_FILE_DELETE, ActionType.ORPHAN_FILE_MOVE}
ORPHAN_TYPES = ORPHAN_FILE_TYPES | {ActionType.ORPHAN_RMDIR}


def new_plan_id() -> str:
    return uuid4().hex[:12]


@dataclass
class Action:
    seq: int
    type: str
    command: str
    target: dict[str, Any]
    reason: str
    params: dict[str, Any] = field(default_factory=dict)
    modifies: bool = False
    deletes: bool = False
    watched: dict[str, Any] = field(default_factory=dict)
    files: list[dict[str, Any]] = field(default_factory=list)
    result: str = ActionResult.PENDING
    message: str | None = None
    checked_at: str | None = None
    finished_at: str | None = None

    @property
    def id(self) -> str:
        return f"a{self.seq + 1}"

    def to_dict(self) -> dict[str, Any]:
        data = {
            "id": self.id,
            "seq": self.seq,
            "type": self.type,
            "command": self.command,
            "target": self.target,
            "reason": self.reason,
            "params": self.params,
            "modifies": self.modifies,
            "deletes": self.deletes,
            "watched": self.watched,
            "files": self.files,
            "result": self.result,
            "message": self.message,
            "checked_at": self.checked_at,
            "finished_at": self.finished_at,
        }
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Action:
        return cls(
            seq=data["seq"],
            type=data["type"],
            command=data["command"],
            target=data.get("target", {}),
            reason=data.get("reason", ""),
            params=data.get("params", {}),
            modifies=data.get("modifies", False),
            deletes=data.get("deletes", False),
            watched=data.get("watched", {}),
            files=data.get("files", []),
            result=data.get("result", ActionResult.PENDING),
            message=data.get("message"),
            checked_at=data.get("checked_at"),
            finished_at=data.get("finished_at"),
        )


def empty_summary() -> dict[str, int]:
    return {
        ActionResult.APPLIED: 0,
        ActionResult.SKIPPED: 0,
        ActionResult.STALE: 0,
        ActionResult.FAILED: 0,
        ActionResult.UNCERTAIN: 0,
    }
