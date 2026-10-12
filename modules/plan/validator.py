"""Precondition re-validation for persisted plans.

Every check is read-only: an action is confirmed only when the qBittorrent
state and filesystem identity captured at preview time still match. Anything
that changed, disappeared, got re-referenced by another torrent or whose
identity (inode/size) changed is reported stale and never executed.
"""

from __future__ import annotations

import math
import os
import unicodedata
from fnmatch import fnmatch
from typing import Any

from modules import util
from modules.plan import snapshot
from modules.plan.models import FILE_SNAPSHOT_TYPES
from modules.plan.models import HASH_TYPES
from modules.plan.models import ORPHAN_FILE_TYPES
from modules.plan.models import WATCHED_KEYS
from modules.plan.models import ActionType
from modules.plan.shadow import parse_tags

logger = util.logger


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _tracker_equal(stored: list[list[Any]], live: list[list[Any]]) -> bool:
    def _norm(rows):
        return sorted((url, status, (msg or "").strip()) for url, status, msg in rows)

    return _norm(stored or []) == _norm(live or [])


def _values_equal(stored: Any, live: Any, key: str) -> bool:
    if key == "tags":
        return set(parse_tags(stored)) == set(parse_tags(live))
    if key == "trackers":
        return _tracker_equal(stored or [], live or [])
    if isinstance(stored, (int, float)) and isinstance(live, (int, float)):
        return math.isclose(float(stored), float(live), rel_tol=1e-9, abs_tol=1e-6)
    return stored == live


class PlanValidator:
    def __init__(self, qbt: Any, config: Any):
        self.qbt = qbt
        self.config = config
        self._live_cache: dict[str, Any | None] = {}
        self._tracked_paths: set[str] | None = None
        self._orphan_threshold_exceeded: bool | None = None

    # ---- plan level ---------------------------------------------------------

    def plan_level(self, plan: dict[str, Any]) -> list[str]:
        reasons = []
        try:
            with open(self.config.config_path, "rb") as fh:
                current_sha = __import__("hashlib").sha256(fh.read()).hexdigest()
            if current_sha != plan.get("config_sha256"):
                reasons.append("Config file content changed since the plan was created")
        except OSError as e:
            reasons.append(f"Config file cannot be read: {e}")
        qbt_info = plan.get("qbt", {})
        if qbt_info.get("host") and qbt_info.get("host") != getattr(self.qbt, "host", None):
            reasons.append("qBittorrent host changed since the plan was created")
        if qbt_info.get("username") and qbt_info.get("username") != getattr(self.qbt, "username", None):
            reasons.append("qBittorrent username changed since the plan was created")
        path_info = plan.get("path", {})
        if path_info.get("root_dir") and path_info.get("root_dir") != self.config.root_dir:
            reasons.append("root_dir changed since the plan was created")
        if path_info.get("remote_dir") and path_info.get("remote_dir") != self.config.remote_dir:
            reasons.append("remote_dir changed since the plan was created")
        return reasons

    # ---- per action ---------------------------------------------------------

    def check_action(self, action: dict[str, Any]) -> tuple[bool, str | None]:
        action_type = action["type"]
        if action_type in HASH_TYPES:
            ok, reason = self._check_torrent_action(action)
            if not ok:
                return False, reason
        if action_type in FILE_SNAPSHOT_TYPES:
            nohl_types = (ActionType.NOHL_ADD, ActionType.NOHL_REMOVE)
            ok, reason = self._check_file_identity(action, compare_nlink=action_type in nohl_types)
            if not ok:
                return False, reason
        if action_type in ORPHAN_FILE_TYPES:
            return self._check_orphan_file(action)
        if action_type == ActionType.ORPHAN_RMDIR:
            return self._check_orphan_rmdir(action)
        if action_type == ActionType.TAG_DEFINITION_DELETE:
            return self._check_tag_definition(action)
        return True, None

    def _live_torrent(self, torrent_hash: str) -> Any | None:
        if torrent_hash not in self._live_cache:
            try:
                rows = self.qbt.get_torrents({"torrent_hashes": torrent_hash})
            except Exception as e:
                logger.warning(f"Plan validation: failed to re-fetch {torrent_hash}: {e}")
                rows = []
            self._live_cache[torrent_hash] = rows[0] if rows else None
        return self._live_cache[torrent_hash]

    def _check_torrent_action(self, action: dict[str, Any]) -> tuple[bool, str | None]:
        torrent_hash = action.get("target", {}).get("hash")
        live = self._live_torrent(torrent_hash)
        if live is None:
            return False, f"Torrent {torrent_hash} no longer exists in qBittorrent"
        live_snapshot = snapshot.torrent_snapshot(live)
        stored = action.get("watched", {})
        for key in WATCHED_KEYS.get(action["type"], []):
            if not _values_equal(stored.get(key), live_snapshot.get(key), key):
                return False, f"Torrent state changed: '{key}' differs from preview"
        return True, None

    def _check_file_identity(self, action: dict[str, Any], compare_nlink: bool) -> tuple[bool, str | None]:
        for f in action.get("files", []):
            current = snapshot.stat_snapshot(f["host_path"], f.get("display_path"))
            if current is None:
                return False, f"File missing since preview: {f.get('display_path')}"
            for key in ("dev", "ino", "size"):
                if current.get(key) != f.get(key):
                    return False, f"File identity changed ({key}): {f.get('display_path')}"
            if compare_nlink and current.get("nlink") != f.get("nlink"):
                return False, f"Hard link count changed: {f.get('display_path')}"
        if not action.get("files"):
            return False, "Preview file state is unavailable; refusing to re-evaluate destructive action"
        return True, None

    # ---- orphaned files -----------------------------------------------------

    def _tracked_root_paths(self) -> set[str]:
        if self._tracked_paths is not None:
            return self._tracked_paths
        tracked = set()
        try:
            for torrent in self.qbt.get_torrents({"sort": "added_on"}):
                if not torrent.state_enum.is_complete and torrent.get("download_path", ""):
                    base_path = torrent["download_path"]
                else:
                    base_path = torrent.save_path
                for f in torrent.files:
                    name = getattr(f, "name", None) or (f.get("name") if isinstance(f, dict) else None)
                    if name:
                        tracked.add(_nfc(os.path.normpath(os.path.join(base_path, name))))
        except Exception as e:
            logger.warning(f"Plan validation: rebuilding tracked file set failed: {e}")
        self._tracked_paths = tracked
        return tracked

    def _orphan_guard(self) -> bool | None:
        """Re-run the mass-deletion threshold guard (True = threshold exceeded)."""
        if self._orphan_threshold_exceeded is not None:
            return self._orphan_threshold_exceeded
        cfg = self.config
        max_files = cfg.orphaned.get("max_orphaned_files_to_delete", -1)
        try:
            root_files = {
                _nfc(f) for f in util.get_root_files(cfg.root_dir, cfg.remote_dir, cfg.orphaned_dir)
            }
            candidates = root_files - self._tracked_root_paths()
            exclude_patterns = [
                util.path_replace(pattern, cfg.remote_dir, cfg.root_dir)
                for pattern in cfg.orphaned.get("exclude_patterns", [])
            ]
            candidates = {f for f in candidates if not any(fnmatch(f, p) for p in exclude_patterns)}
            self._orphan_threshold_exceeded = bool(
                max_files not in (None, -1) and len(candidates) > max_files
            )
        except Exception as e:
            logger.warning(f"Plan validation: orphan threshold guard failed: {e}")
            # Fail closed: when the guard cannot be evaluated, do not allow
            # destructive orphan actions.
            self._orphan_threshold_exceeded = True
        return self._orphan_threshold_exceeded

    def _check_orphan_file(self, action: dict[str, Any]) -> tuple[bool, str | None]:
        files = action.get("files", [])
        if not files:
            return False, "Preview file state is unavailable for orphan action"
        f = files[0]
        current = snapshot.stat_snapshot(f["host_path"], f.get("display_path"))
        if current is None:
            return False, f"Orphan file no longer exists: {f.get('display_path')}"
        for key in ("dev", "ino", "size"):
            if current.get(key) != f.get(key):
                return False, f"Orphan file identity changed ({key}): {f.get('display_path')}"
        root_namespace = f.get("display_path")
        if _nfc(os.path.normpath(root_namespace)) in self._tracked_root_paths():
            return False, f"File is referenced by a torrent again: {root_namespace}"
        if self._orphan_guard():
            return False, "Orphan cleanup threshold is currently exceeded; action invalidated"
        if action["type"] == ActionType.ORPHAN_FILE_MOVE:
            dest = action.get("params", {}).get("dest")
            if dest and os.path.exists(dest):
                return False, f"Move destination already exists: {dest}"
        return True, None

    def _check_orphan_rmdir(self, action: dict[str, Any]) -> tuple[bool, str | None]:
        directory = action.get("params", {}).get("dir")
        if not directory or not os.path.isdir(directory):
            return False, f"Directory no longer exists: {directory}"
        try:
            if os.listdir(directory):
                return False, f"Directory is no longer empty: {directory}"
        except OSError as e:
            return False, f"Directory cannot be read: {e}"
        save_paths = self.qbt.get_category_save_paths()
        save_path_values = save_paths.values() if hasattr(save_paths, "values") else save_paths
        excluded = {os.path.normcase(os.path.normpath(p)) for p in save_path_values}
        norm_dir = os.path.normcase(os.path.normpath(directory))
        if any(norm_dir == p or norm_dir.startswith(p + os.sep) for p in excluded):
            return False, f"Directory belongs to a category save path: {directory}"
        return True, None

    def _check_tag_definition(self, action: dict[str, Any]) -> tuple[bool, str | None]:
        tag = action.get("params", {}).get("tag")
        try:
            existing = list(self.qbt.client.torrent_tags.tags)
        except Exception as e:
            return False, f"Cannot read qBittorrent tags: {e}"
        if tag not in existing:
            return False, f"Tag definition already removed: {tag}"
        return True, None
