"""Read-only state snapshot helpers for plan capture and validation.

Snapshots capture the exact qBittorrent fields and filesystem identity
(dev/inode/size/mtime/nlink) an action depends on, so confirmation can detect
any drift before performing the action.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from modules import util

logger = util.logger

# Torrent fields copied into every action's precondition snapshot. Per-action
# watched-key lists (models.WATCHED_KEYS) decide which fields matter at
# confirmation; the rest are kept for audit/display.
TORRENT_FIELDS = [
    "hash",
    "name",
    "category",
    "tags",
    "state",
    "save_path",
    "content_path",
    "auto_tmm",
    "ratio",
    "ratio_limit",
    "seeding_time_limit",
    "inactive_seeding_time_limit",
    "up_limit",
    "progress",
    "seeding_time",
    "num_complete",
    "last_activity",
]


def _read(torrent: Any, key: str) -> Any:
    try:
        value = getattr(torrent, key)
    except Exception:
        try:
            value = torrent.get(key)
        except Exception:
            return None
    if key == "tags" and value is None:
        return ""
    return value


def torrent_snapshot(torrent: Any) -> dict[str, Any]:
    snapshot = {key: _read(torrent, key) for key in TORRENT_FIELDS}
    snapshot["trackers"] = tracker_snapshot(torrent)
    return snapshot


def tracker_snapshot(torrent: Any) -> list[list[Any]]:
    try:
        trackers = torrent.trackers
    except Exception:
        return []
    result = []
    for trk in trackers or []:
        url = getattr(trk, "url", None) or (trk.get("url") if isinstance(trk, dict) else None)
        if not url or url.split(":")[0] not in ("http", "https", "udp", "ws", "wss"):
            continue
        status = getattr(trk, "status", None)
        if status is None and isinstance(trk, dict):
            status = trk.get("status")
        msg = getattr(trk, "msg", None)
        if msg is None and isinstance(trk, dict):
            msg = trk.get("msg")
        result.append([url, status, msg or ""])
    return result


def host_save_path(torrent: Any, root_dir: str, remote_dir: str) -> str:
    return util.path_replace(_read(torrent, "save_path") or "", root_dir, remote_dir)


def _host_and_display(host_path: str, remote_dir: str, root_dir: str) -> tuple[str, str]:
    display = util.path_replace(host_path, remote_dir, root_dir)
    return host_path, display


def stat_snapshot(host_path: str, display_path: str | None = None) -> dict[str, Any] | None:
    try:
        st = os.stat(host_path)
    except (FileNotFoundError, PermissionError, OSError) as e:
        logger.debug(f"Plan snapshot: cannot stat {host_path}: {e}")
        return None
    return {
        "host_path": host_path,
        "display_path": display_path if display_path is not None else host_path,
        "dev": st.st_dev,
        "ino": st.st_ino,
        "size": st.st_size,
        "mtime": st.st_mtime,
        "nlink": st.st_nlink,
    }


def _iter_torrent_host_files(torrent: Any, root_dir: str, remote_dir: str) -> list[tuple[str, str]]:
    """Return (host_path, display_path) for every file in a torrent."""
    save_host = host_save_path(torrent, root_dir, remote_dir)
    files = []
    for f in torrent.files or []:
        name = getattr(f, "name", None) or (f.get("name") if isinstance(f, dict) else None)
        if not name:
            continue
        host = os.path.normpath(os.path.join(save_host, name))
        files.append(_host_and_display(host, remote_dir, root_dir))
    return files


def content_file_snapshots(torrent: Any, root_dir: str, remote_dir: str) -> list[dict[str, Any]]:
    """Stat identity of every file belonging to a torrent (for deletes)."""
    snapshots = []
    for host, display in _iter_torrent_host_files(torrent, root_dir, remote_dir):
        snap = stat_snapshot(host, display)
        if snap is not None:
            snapshots.append(snap)
    return snapshots


def nohl_file_snapshots(torrent: Any, root_dir: str, remote_dir: str) -> list[dict[str, Any]]:
    """Stat identity of the files the no-hardlink verdict is based on.

    Mirrors CheckHardLinks.nohardlink(): a single file checks itself; a folder
    checks regular files at or above 10% of the largest file's size.
    """
    content_host = util.path_replace(_read(torrent, "content_path") or "", root_dir, remote_dir)
    if not content_host:
        return []
    try:
        if os.path.isfile(content_host):
            snap = stat_snapshot(content_host, util.path_replace(content_host, remote_dir, root_dir))
            return [snap] if snap is not None else []
        if not os.path.isdir(content_host):
            return []
        regular = [p for p in Path(content_host).rglob("*") if not p.is_symlink() and p.is_file()]
        if not regular:
            return []
        regular.sort(key=lambda p: p.stat().st_size, reverse=True)
        threshold = regular[0].stat().st_size * 0.1
        snapshots = []
        for p in regular:
            st = p.stat()
            if st.st_size < threshold:
                break
            snap = stat_snapshot(str(p), util.path_replace(str(p), remote_dir, root_dir))
            if snap is not None:
                snapshots.append(snap)
        return snapshots
    except (FileNotFoundError, PermissionError, OSError) as e:
        logger.debug(f"Plan noHL snapshot failed for {content_host}: {e}")
        return []


def orphan_file_snapshot(host_path: str, display_path: str) -> dict[str, Any] | None:
    return stat_snapshot(host_path, display_path)
