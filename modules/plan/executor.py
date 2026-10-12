"""Execute a confirmed plan: re-validate each action, perform it, reconcile.

Only pending actions are processed. Every outcome is persisted immediately so
a crashed process resumes without re-deciding or re-applying finished work.
Destructive operations are reconciled against qBittorrent and disk truth when
the API response is missing, timed out or otherwise uncertain.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from modules import util
from modules.plan.models import ActionResult
from modules.plan.models import ActionType
from modules.plan.models import empty_summary
from modules.plan.shadow import parse_tags
from modules.plan.validator import PlanValidator

logger = util.logger


def _log_exception(exc: Exception) -> None:
    try:
        logger.stacktrace()
    except Exception:
        try:
            logger.error(str(exc))
        except Exception:
            pass


TAG_ADD_TYPES = {
    ActionType.TAGS_ADD,
    ActionType.NOHL_ADD,
    ActionType.TRACKER_ERROR_ADD,
    ActionType.SHARE_TAG_ADD,
}
TAG_REMOVE_TYPES = {
    ActionType.TAGS_REMOVE,
    ActionType.NOHL_REMOVE,
    ActionType.TRACKER_ERROR_REMOVE,
    ActionType.SHARE_TAG_REMOVE,
}
DELETE_TYPES = {ActionType.UNREG_DELETE, ActionType.SHARE_CLEANUP_DELETE}


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


class PlanExecutor:
    def __init__(self, plan: dict[str, Any], qbt: Any, config: Any, store: Any):
        self.plan = plan
        self.qbt = qbt
        self.config = config
        self.store = store
        self.validator = PlanValidator(qbt, config)

    def run(self, skip_ids: set[str] | None = None) -> dict[str, Any]:
        skip_ids = skip_ids or set()
        self.plan["status"] = "running"
        self._save()
        for action in self.plan["actions"]:
            if action.get("result") != ActionResult.PENDING:
                continue
            action["checked_at"] = _now()
            if action.get("id") in skip_ids:
                action["result"] = ActionResult.SKIPPED
                action["message"] = "Skipped by operator"
            else:
                ok, reason = self.validator.check_action(action)
                if not ok:
                    action["result"] = ActionResult.STALE
                    action["message"] = reason
                    logger.info(f"Plan action {action['id']} stale: {reason}")
                else:
                    result, message = self._perform(action)
                    action["result"] = result
                    action["message"] = message
            action["finished_at"] = _now()
            self._save()

        summary = empty_summary()
        for action in self.plan["actions"]:
            if action["result"] in summary:
                summary[action["result"]] += 1
        self.plan["summary"] = summary
        self.plan["status"] = (
            "completed_with_failures"
            if summary[ActionResult.FAILED] or summary[ActionResult.UNCERTAIN]
            else "completed"
        )
        self._save()
        self._send_summary_notification()
        return self.plan

    def _save(self) -> None:
        self.plan["heartbeat_at"] = _now()
        self.plan["updated_at"] = _now()
        self.store.save(self.plan)

    # ---- dispatch -----------------------------------------------------------

    def _perform(self, action: dict[str, Any]) -> tuple[str, str | None]:
        action_type = action["type"]
        try:
            if action_type in TAG_ADD_TYPES or action_type in TAG_REMOVE_TYPES:
                return self._perform_tags(action, add=action_type in TAG_ADD_TYPES)
            if action_type == ActionType.CATEGORY_SET:
                return self._perform_category_set(action)
            if action_type == ActionType.CATEGORY_ENABLE_TMM:
                return self._perform_simple(action, "set_auto_management", True, verify_key="auto_tmm", expected=True)
            if action_type in (ActionType.RESUME, ActionType.SHARE_RESUME):
                return self._perform_simple(action, "resume", verify_key=None)
            if action_type == "pause":
                return self._perform_simple(action, "pause", verify_key=None)
            if action_type == ActionType.RECHECK:
                return self._perform_simple(action, "recheck", verify_key=None)
            if action_type == ActionType.SET_SHARE_LIMITS:
                return self._perform_set_share_limits(action)
            if action_type == ActionType.SET_UPLOAD_LIMIT:
                return self._perform_set_upload_limit(action)
            if action_type in DELETE_TYPES:
                return self._perform_delete(action)
            if action_type == ActionType.ORPHAN_FILE_DELETE:
                return self._perform_orphan_delete(action)
            if action_type == ActionType.ORPHAN_FILE_MOVE:
                return self._perform_orphan_move(action)
            if action_type == ActionType.ORPHAN_RMDIR:
                return self._perform_orphan_rmdir(action)
            if action_type == ActionType.TAG_DEFINITION_DELETE:
                return self._perform_tag_definition_delete(action)
            return ActionResult.FAILED, f"Unknown action type: {action_type}"
        except Exception as e:
            _log_exception(e)
            return ActionResult.FAILED, f"{type(e).__name__}: {e}"

    def _live(self, action: dict[str, Any]) -> Any:
        torrent_hash = action["target"]["hash"]
        self.validator._live_cache.pop(torrent_hash, None)
        rows = self.qbt.get_torrents({"torrent_hashes": torrent_hash})
        live = rows[0] if rows else None
        self.validator._live_cache[torrent_hash] = live
        return live

    def _perform_tags(self, action: dict[str, Any], *, add: bool) -> tuple[str, str | None]:
        live = self._live(action)
        if live is None:
            return ActionResult.FAILED, "Torrent vanished between validation and execution"
        tags = action["params"]["tags"]
        try:
            if add:
                live.add_tags(tags)
            else:
                live.remove_tags(tags)
        except Exception as e:
            return ActionResult.FAILED, f"Tag update API error: {e}"
        live = self._live(action)
        current = set(parse_tags(getattr(live, "tags", "")))
        if add and not set(tags).issubset(current):
            return ActionResult.FAILED, "Tags missing after add"
        if not add and set(tags) & current:
            return ActionResult.FAILED, "Tags still present after removal"
        return ActionResult.APPLIED, None

    def _perform_category_set(self, action: dict[str, Any]) -> tuple[str, str | None]:
        live = self._live(action)
        if live is None:
            return ActionResult.FAILED, "Torrent vanished between validation and execution"
        category = action["params"]["category"]
        save_path = action["params"].get("save_path") or live.save_path
        try:
            try:
                live.set_category(category=category)
            except Exception as e:
                # Mirrors category.py: a missing category is created on demand.
                message = str(e).lower()
                if "not found" in message or "conflict" in type(e).__name__.lower() or "incorrect category" in message:
                    self.qbt.client.torrent_categories.create_category(name=category, save_path=save_path)
                    live.set_category(category=category)
                else:
                    raise
        except Exception as e:
            return ActionResult.FAILED, f"Category update API error: {e}"
        live = self._live(action)
        if getattr(live, "category", None) != category:
            return ActionResult.FAILED, "Category not applied"
        return ActionResult.APPLIED, None

    def _perform_simple(self, action, method_name, arg=None, *, verify_key, expected=None) -> tuple[str, str | None]:
        live = self._live(action)
        if live is None:
            return ActionResult.FAILED, "Torrent vanished between validation and execution"
        method = getattr(live, method_name)
        try:
            method() if arg is None else method(arg)
        except Exception as e:
            return ActionResult.FAILED, f"{method_name} API error: {e}"
        if verify_key is None:
            return ActionResult.APPLIED, None
        live = self._live(action)
        value = _field(live, verify_key)
        if value != expected:
            return ActionResult.FAILED, f"{verify_key} not applied"
        return ActionResult.APPLIED, None

    def _perform_set_share_limits(self, action: dict[str, Any]) -> tuple[str, str | None]:
        live = self._live(action)
        if live is None:
            return ActionResult.FAILED, "Torrent vanished between validation and execution"
        p = action["params"]
        try:
            live.set_share_limits(
                ratio_limit=p["ratio_limit"],
                seeding_time_limit=p["seeding_time_limit"],
                inactive_seeding_time_limit=p.get("inactive_seeding_time_limit", -2),
                share_limit_action=p.get("share_limit_action"),
            )
        except Exception as e:
            return ActionResult.FAILED, f"set_share_limits API error: {e}"
        live = self._live(action)
        if _field(live, "ratio_limit") != p["ratio_limit"] or _field(live, "seeding_time_limit") != p["seeding_time_limit"]:
            return ActionResult.FAILED, "Share limits not applied"
        return ActionResult.APPLIED, None

    def _perform_set_upload_limit(self, action: dict[str, Any]) -> tuple[str, str | None]:
        live = self._live(action)
        if live is None:
            return ActionResult.FAILED, "Torrent vanished between validation and execution"
        limit = action["params"]["limit"]
        try:
            live.set_upload_limit(limit)
        except Exception as e:
            return ActionResult.FAILED, f"set_upload_limit API error: {e}"
        live = self._live(action)
        expected = 0 if limit == -1 else limit
        if _field(live, "up_limit") != expected:
            return ActionResult.FAILED, "Upload limit not applied"
        return ActionResult.APPLIED, None

    def _perform_tag_definition_delete(self, action: dict[str, Any]) -> tuple[str, str | None]:
        tag = action["params"]["tag"]
        try:
            self.qbt.client.torrent_tags.delete_tags(tag)
            existing = list(self.qbt.client.torrent_tags.tags)
        except Exception as e:
            return ActionResult.FAILED, f"Tag definition delete API error: {e}"
        if tag in existing:
            return ActionResult.FAILED, "Tag definition still exists"
        return ActionResult.APPLIED, None

    # ---- destructive operations with reconciliation ------------------------

    @staticmethod
    def _has_working_tracker(live: Any) -> bool:
        try:
            from qbittorrentapi import TrackerStatus

            return any(
                trk.url.split(":")[0] in ("http", "https", "udp", "ws", "wss")
                and TrackerStatus(trk.status) == TrackerStatus.WORKING
                for trk in live.trackers
            )
        except Exception:
            return False

    def _recompute_delete_attr(self, live: Any, stored_attr: dict) -> dict:
        attr = dict(stored_attr or {})
        content_host = util.path_replace(live["content_path"], self.config.root_dir, self.config.remote_dir)
        delete_contents = True
        if not os.path.exists(content_host):
            delete_contents = False
        elif self.qbt.has_cross_seed(live) and self._has_working_tracker(live):
            # Cross-seeded content with a working tracker is preserved.
            delete_contents = False
        attr["torrents_deleted_and_contents"] = delete_contents
        return attr

    def _perform_delete(self, action: dict[str, Any]) -> tuple[str, str | None]:
        live = self._live(action)
        if live is None:
            return ActionResult.FAILED, "Torrent vanished between validation and execution"
        attr = self._recompute_delete_attr(live, action["params"].get("attr", {}))
        delete_contents = bool(attr.get("torrents_deleted_and_contents"))
        error = None
        try:
            self.qbt.tor_delete_recycle(live, attr)
        except Exception as e:
            _log_exception(e)
            error = e
        return self._reconcile_torrent_delete(action, delete_contents, error)

    def _reconcile_torrent_delete(
        self, action: dict[str, Any], delete_contents: bool, error: Exception | None
    ) -> tuple[str, str | None]:
        torrent_hash = action["target"]["hash"]
        try:
            still = self.qbt.get_torrents({"torrent_hashes": torrent_hash})
            gone = not still
        except Exception as query_error:
            return (
                ActionResult.UNCERTAIN,
                f"Delete outcome unprovable (query failed: {query_error}; original error: {error})",
            )
        if not gone:
            return ActionResult.FAILED, f"Torrent still exists after delete attempt ({error})"
        if not delete_contents:
            return ActionResult.APPLIED, None
        missing = []
        try:
            for f in action.get("files", []):
                if os.path.exists(f["host_path"]):
                    missing.append(f.get("display_path"))
        except OSError as e:
            return ActionResult.UNCERTAIN, f"Torrent gone but disk state unreadable: {e}"
        if missing:
            return (
                ActionResult.UNCERTAIN,
                "Torrent removed but content remains for: " + ", ".join(missing[:10]),
            )
        return ActionResult.APPLIED, None

    def _perform_orphan_delete(self, action: dict[str, Any]) -> tuple[str, str | None]:
        src = action["params"]["src"]
        try:
            util.delete_files(src)
        except Exception as e:
            _log_exception(e)
            return self._reconcile_orphan_file(src, None, e)
        return self._reconcile_orphan_file(src, None, None)

    def _perform_orphan_move(self, action: dict[str, Any]) -> tuple[str, str | None]:
        src, dest = action["params"]["src"], action["params"]["dest"]
        try:
            util.move_files(src, dest, True)
        except Exception as e:
            _log_exception(e)
            return self._reconcile_orphan_file(src, dest, e)
        return self._reconcile_orphan_file(src, dest, None)

    def _reconcile_orphan_file(self, src: str, dest: str | None, error: Exception | None) -> tuple[str, str | None]:
        try:
            src_exists = os.path.exists(src)
            dest_exists = dest is not None and os.path.exists(dest)
        except OSError as e:
            return ActionResult.UNCERTAIN, f"File outcome unprovable: {e}"
        if dest is not None:
            if not src_exists and dest_exists:
                return ActionResult.APPLIED, None
            if src_exists and not dest_exists:
                return ActionResult.FAILED, f"Move not completed ({error})"
            return ActionResult.UNCERTAIN, f"Ambiguous move state for {src} ({error})"
        if not src_exists:
            return ActionResult.APPLIED, None
        return ActionResult.FAILED, f"File still exists after delete attempt ({error})"

    def _perform_orphan_rmdir(self, action: dict[str, Any]) -> tuple[str, str | None]:
        directory = action["params"]["dir"]
        exclude_patterns = [
            util.path_replace(p, self.config.remote_dir, self.config.root_dir)
            for p in self.config.orphaned.get("exclude_patterns", [])
        ]
        try:
            util.remove_empty_directories(directory, self.qbt.get_category_save_paths(), exclude_patterns)
        except Exception as e:
            return ActionResult.FAILED, f"rmdir error: {e}"
        if os.path.isdir(directory):
            return ActionResult.FAILED, "Directory still exists after cleanup attempt"
        return ActionResult.APPLIED, None

    # ---- summary notification ----------------------------------------------

    def _send_summary_notification(self) -> None:
        summary = self.plan["summary"]
        total = sum(summary.values())
        if total == 0:
            return
        try:
            factory = self.config.webhooks_factory
            uncertain_actions = [
                a for a in self.plan["actions"] if a["result"] == ActionResult.UNCERTAIN
            ]
            body = (
                f"Execution plan {self.plan['plan_id']} ({self.plan['config_file']}) finished.\n"
                f"Applied: {summary[ActionResult.APPLIED]}\n"
                f"Skipped: {summary[ActionResult.SKIPPED]}\n"
                f"Stale (not executed): {summary[ActionResult.STALE]}\n"
                f"Failed: {summary[ActionResult.FAILED]}\n"
                f"Uncertain (manual check required): {summary[ActionResult.UNCERTAIN]}"
            )
            if uncertain_actions:
                body += "\nManual review:\n" + "\n".join(
                    f"- {a['id']} {a['type']} {a.get('target', {}).get('name', '')}: {a.get('message', '')}"
                    for a in uncertain_actions[:20]
                )
            stats = {
                "added": 0,
                "deleted": sum(
                    1 for a in self.plan["actions"] if a["type"] in DELETE_TYPES and a["result"] == ActionResult.APPLIED
                ),
                "deleted_contents": sum(
                    1
                    for a in self.plan["actions"]
                    if a["type"] in DELETE_TYPES
                    and a["result"] == ActionResult.APPLIED
                    and a.get("params", {}).get("attr", {}).get("torrents_deleted_and_contents")
                ),
                "resumed": 0,
                "rechecked": 0,
                "categorized": 0,
                "tagged": 0,
                "rem_unreg": 0,
                "tagged_tracker_error": 0,
                "untagged_tracker_error": 0,
                "orphaned": sum(
                    1
                    for a in self.plan["actions"]
                    if a["type"] in (ActionType.ORPHAN_FILE_DELETE, ActionType.ORPHAN_FILE_MOVE)
                    and a["result"] == ActionResult.APPLIED
                ),
                "tagged_noHL": 0,
                "untagged_noHL": 0,
                "recycle_emptied": 0,
                "orphaned_emptied": 0,
                "updated_share_limits": 0,
                "cleaned_share_limits": 0,
            }
            start = datetime.utcnow()
            factory.end_time_hooks(start, start, "0:00:00", None, stats, body)
        except Exception as e:
            logger.warning(f"Plan summary notification failed: {e}")


def _field(obj: Any, key: str) -> Any:
    try:
        return getattr(obj, key)
    except Exception:
        try:
            return obj.get(key)
        except Exception:
            return None
