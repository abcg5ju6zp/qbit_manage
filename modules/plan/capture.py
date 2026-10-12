"""Capture a dry preview into a structured, persisted execution plan.

PlanCapture installs shadow torrents/clients around a live Qbt manager so the
unchanged core decision modules run to completion without touching qBittorrent
or the filesystem, collecting one Action per mutating call. Capture is
transparent when ``config.plan is None`` (CLI / scheduler / normal dry-run).
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime
from typing import Any

from modules import util
from modules.plan import snapshot
from modules.plan.models import SCHEMA_VERSION
from modules.plan.models import Action
from modules.plan.models import ActionType
from modules.plan.models import empty_summary
from modules.plan.models import new_plan_id
from modules.plan.shadow import ADD_TAGS_TYPES
from modules.plan.shadow import REMOVE_TAGS_TYPES
from modules.plan.shadow import ShadowTorrent
from modules.plan.shadow import unwrap

logger = util.logger


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


class PlanCapture:
    def __init__(self, config: Any, store: Any | None = None):
        self.config = config
        self.qbt = config.qbt
        self.client = config.qbt.client
        self.store = store
        self.actions: list[Action] = []
        self.command = ""
        self._reason = ""
        self.plan_id = new_plan_id()
        self._restorables: list[tuple[Any, str, Any]] = []
        self._shadows: dict[Any, ShadowTorrent] = {}
        self._orphan_rmdirs: set[str] = set()
        self._installed = False

    # ---- module-facing context helpers -------------------------------------

    def set_context(self, command: str) -> None:
        self.command = command

    def set_reason(self, reason: str) -> None:
        self._reason = reason or ""

    def take_reason(self) -> str:
        reason = self._reason
        self._reason = ""
        return reason

    # ---- installation -------------------------------------------------------

    def install(self) -> None:
        if self._installed:
            return
        cfg = self.config
        # get_torrents returns memoized shadows so single-hash re-fetches in
        # share_limits observe the same in-memory simulated state.
        original_get_torrents = self.qbt.get_torrents

        def shadow_get_torrents(params=None):
            return [self._wrap(t) for t in original_get_torrents(params or {})]

        self._replace(self.qbt, "get_torrents", shadow_get_torrents)

        try:
            original_list = list(self.qbt.torrent_list)
            self._replace(self.qbt, "torrent_list", [self._wrap(t) for t in original_list])
        except Exception:
            # _replace appended a restorable tuple before the failed setattr;
            # read-only properties (test doubles) cannot be restored anyway.
            if self._restorables and self._restorables[-1][1] == "torrent_list":
                self._restorables.pop()
            # Test doubles may expose torrents via a read-only property/_torrents.
            if hasattr(self.qbt, "_torrents"):
                original_torrents = list(self.qbt._torrents)
                self._restorables.append((self.qbt, "_torrents", original_torrents))
                self.qbt._torrents = [self._wrap(t) for t in original_torrents]
            for attr_name in ("_torrentissue_override", "_torrentvalid_override"):
                override = getattr(self.qbt, attr_name, None)
                if isinstance(override, list):
                    self._restorables.append((self.qbt, attr_name, list(override)))
                    setattr(self.qbt, attr_name, [self._wrap(t) for t in override])

        # Rebuild torrentinfo/torrentissue/torrentvalid from shadows while
        # dry_run still suppresses the force_auto_tmm write in get_torrent_info.
        if callable(getattr(self.qbt, "get_torrent_info", None)) and hasattr(self.qbt, "torrentfiles"):
            self.qbt.torrentfiles = {}
            try:
                self.qbt.get_torrent_info()
            except Exception as e:
                logger.warning(f"Plan capture: rebuilding torrent info failed: {e}")

        # From here on module code must believe this is a real run so that
        # decisions (esp. share_limits tag switching) match real execution.
        self._restorables.append((cfg, "dry_run", cfg.dry_run))
        cfg.dry_run = False

        self._replace(self.qbt, "tor_delete_recycle", self._shadow_tor_delete_recycle)
        self._replace(
            self.client.torrent_tags,
            "delete_tags",
            lambda tag=None, **kwargs: self._record_tag_definition_delete(tag),
        )
        self._replace(
            self.client.torrent_categories,
            "create_category",
            lambda name=None, save_path=None, **kwargs: None,
        )
        self._silence_notifications()
        self._installed = True

    def _wrap(self, torrent: Any) -> ShadowTorrent:
        if isinstance(torrent, ShadowTorrent):
            return torrent
        key = getattr(torrent, "hash", None) or id(torrent)
        shadow = self._shadows.get(key)
        if shadow is None:
            shadow = ShadowTorrent(torrent, self)
            self._shadows[key] = shadow
        return shadow

    def _replace(self, obj: Any, attr: str, value: Any) -> None:
        self._restorables.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, value)

    def _silence_notifications(self) -> None:
        cfg = self.config
        self._replace(cfg, "send_notifications", lambda *a, **k: None)
        self._replace(cfg, "notify", lambda *a, **k: None)
        factory = getattr(cfg, "webhooks_factory", None)
        if factory is not None:
            for method in ("notify", "error_hooks", "start_time_hooks", "end_time_hooks"):
                if callable(getattr(factory, method, None)):
                    self._replace(factory, method, lambda *a, **k: None)

    def restore(self) -> None:
        if not self._installed:
            return
        for obj, attr, original in reversed(self._restorables):
            try:
                setattr(obj, attr, original)
            except Exception as e:
                logger.warning(f"Plan capture: failed to restore {attr}: {e}")
        self._restorables = []
        self._installed = False

    # ---- action recording ---------------------------------------------------

    def _new_action(self, action_type: str, **kwargs) -> Action:
        action = Action(seq=len(self.actions), type=action_type, **kwargs)
        self.actions.append(action)
        return action

    def record_primitive(
        self,
        shadow_torrent: Any,
        primitive: str,
        params: dict,
        default_reason: str,
        watched: dict | None = None,
    ) -> None:
        torrent = unwrap(shadow_torrent)
        action_type = self._primitive_type(primitive)
        reason = self.take_reason() or default_reason
        root_dir, remote_dir = self.config.root_dir, self.config.remote_dir

        files = []
        if action_type in (ActionType.NOHL_ADD, ActionType.NOHL_REMOVE):
            files = snapshot.nohl_file_snapshots(torrent, root_dir, remote_dir)

        # watched must describe the PRE-mutation state; shadows capture it
        # before mirroring and pass it explicitly.
        self._new_action(
            action_type,
            command=self.command,
            target={"hash": torrent.hash, "name": torrent.name},
            reason=reason,
            params=params,
            modifies=True,
            deletes=False,
            watched=watched if watched is not None else snapshot.torrent_snapshot(torrent),
            files=files,
        )

    def _primitive_type(self, primitive: str) -> str:
        context = self.command
        if primitive == "add_tags":
            return ADD_TAGS_TYPES.get(context, ActionType.TAGS_ADD)
        if primitive == "remove_tags":
            return REMOVE_TAGS_TYPES.get(context, ActionType.TAGS_REMOVE)
        if primitive == "set_category":
            return ActionType.CATEGORY_SET
        if primitive == "set_auto_management":
            return ActionType.CATEGORY_ENABLE_TMM
        if primitive == "set_share_limits":
            return ActionType.SET_SHARE_LIMITS
        if primitive == "set_upload_limit":
            return ActionType.SET_UPLOAD_LIMIT
        if primitive == "recheck":
            return ActionType.RECHECK
        if primitive == "resume":
            return ActionType.RESUME if context == "recheck" else ActionType.SHARE_RESUME
        if primitive == "pause":
            return "pause"
        if primitive == "delete":
            # Defensive fallback: production deletes go through tor_delete_recycle.
            return ActionType.UNREG_DELETE if context == "rem_unregistered" else ActionType.SHARE_CLEANUP_DELETE
        raise ValueError(f"Unknown shadow primitive: {primitive}")

    def _shadow_tor_delete_recycle(self, shadow_torrent: Any, info: dict) -> None:
        torrent = unwrap(shadow_torrent)
        function = info.get("function")
        if function == "rem_unregistered":
            action_type, command = ActionType.UNREG_DELETE, "rem_unregistered"
            default_reason = f"Tracker reports torrent as unregistered: {info.get('torrent_status', '')}".strip()
        else:
            action_type, command = ActionType.SHARE_CLEANUP_DELETE, "share_limits"
            default_reason = "Torrent reached configured share limit cleanup threshold"

        root_dir, remote_dir = self.config.root_dir, self.config.remote_dir
        content_host = util.path_replace(torrent["content_path"], root_dir, remote_dir)
        reason = self.take_reason() or default_reason
        self._new_action(
            action_type,
            command=command,
            target={"hash": torrent.hash, "name": torrent.name, "paths": [content_host]},
            reason=reason,
            params={"attr": info},
            modifies=True,
            deletes=bool(info.get("torrents_deleted_and_contents")),
            watched=snapshot.torrent_snapshot(torrent),
            files=snapshot.content_file_snapshots(torrent, root_dir, remote_dir),
        )

    def _record_tag_definition_delete(self, tag: str) -> None:
        self._new_action(
            ActionType.TAG_DEFINITION_DELETE,
            command="share_limits",
            target={"name": tag},
            reason=self.take_reason() or f"Remove obsolete share limits tag definition: {tag}",
            params={"tag": tag},
            modifies=True,
            deletes=False,
            watched={},
        )

    def record_orphan_files(self, orphaned_files: list[str]) -> None:
        """Record one action per orphaned file plus parent directory cleanups."""
        self.command = "rem_orphaned"
        cfg = self.config
        move_mode = cfg.orphaned["empty_after_x_days"] != 0
        for file in orphaned_files:
            src = util.path_replace(file, cfg.root_dir, cfg.remote_dir)
            dest = os.path.join(cfg.orphaned_dir, util.path_replace(file, cfg.root_dir, ""))
            snap = snapshot.orphan_file_snapshot(src, file)
            action_type = ActionType.ORPHAN_FILE_MOVE if move_mode else ActionType.ORPHAN_FILE_DELETE
            self._new_action(
                action_type,
                command="rem_orphaned",
                target={"paths": [file]},
                reason="File is not referenced by any qBittorrent torrent",
                params={"src": src, "dest": dest},
                modifies=True,
                deletes=not move_mode,
                watched={"path": src, "stat": snap},
                files=[snap] if snap else [],
            )
            self._orphan_rmdirs.add(util.path_replace(os.path.dirname(file), cfg.root_dir, cfg.remote_dir))
        for parent in sorted(self._orphan_rmdirs):
            display = util.path_replace(parent, cfg.remote_dir, cfg.root_dir)
            self._new_action(
                ActionType.ORPHAN_RMDIR,
                command="rem_orphaned",
                target={"paths": [display]},
                reason="Remove directories left empty after orphan cleanup",
                params={"dir": parent},
                modifies=True,
                deletes=False,
                watched={"dir": parent},
            )

    # ---- finalization -------------------------------------------------------

    def finalize(self, commands: list[str], hashes: list[str]) -> dict[str, Any]:
        cfg = self.config
        config_text = ""
        try:
            with open(cfg.config_path, encoding="utf-8") as fh:
                config_text = fh.read()
        except OSError as e:
            logger.warning(f"Plan capture: could not read config for freezing: {e}")

        plan = {
            "plan_id": self.plan_id,
            "schema_version": SCHEMA_VERSION,
            "config_file": os.path.basename(cfg.config_path),
            "config_text": config_text,
            "config_sha256": hashlib.sha256(config_text.encode("utf-8")).hexdigest(),
            "qbt": {
                "host": getattr(self.qbt, "host", None),
                "username": getattr(self.qbt, "username", None),
            },
            "path": {"root_dir": cfg.root_dir, "remote_dir": cfg.remote_dir},
            "commands": list(commands),
            "hashes": list(hashes or []),
            "created_at": _now(),
            "updated_at": _now(),
            "heartbeat_at": None,
            "status": "pending",
            "summary": empty_summary(),
            "actions": [action.to_dict() for action in self.actions],
        }
        if self.store is not None:
            self.store.save(plan)
        return plan
