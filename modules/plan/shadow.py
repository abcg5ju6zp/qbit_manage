"""Shadow torrent wrapper used while capturing an execution plan.

The core modules' decision code runs unchanged during capture. Every mutating
qBittorrent call lands on a ShadowTorrent instead of the network: it records a
planned action and mirrors the resulting state on the wrapped torrent object,
so subsequent in-process decisions (tag-driven group assignment, single-hash
re-fetches, etc.) see exactly what a real run would have produced.
"""

from __future__ import annotations

from typing import Any

from modules.plan.snapshot import torrent_snapshot

# Mapping of primitive mutator calls to plan action types per command context.
ADD_TAGS_TYPES = {
    "tag_update": "tags_add",
    "tag_nohardlinks": "nohl_add",
    "rem_unregistered": "tracker_error_add",
    "tag_tracker_error": "tracker_error_add",
    "share_limits": "share_tag_add",
}
REMOVE_TAGS_TYPES = {
    "tag_update": "tags_remove",
    "tag_nohardlinks": "nohl_remove",
    "rem_unregistered": "tracker_error_remove",
    "tag_tracker_error": "tracker_error_remove",
    "share_limits": "share_tag_remove",
}


def parse_tags(tags: Any) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        return [t.strip() for t in tags.split(",") if t.strip()]
    return [str(t).strip() for t in tags if str(t).strip()]


def tags_to_string(tags: list[str]) -> str:
    return ", ".join(tags)


class ShadowTorrent:
    def __init__(self, real: Any, capture: Any):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_capture", capture)

    # ---- read forwarding ----------------------------------------------------

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_real"), name)

    def __getitem__(self, key):
        real = object.__getattribute__(self, "_real")
        return real[key]

    def get(self, key, default=None):
        real = object.__getattribute__(self, "_real")
        try:
            return real.get(key, default)
        except Exception:
            return getattr(real, key, default)

    def __eq__(self, other):
        real = object.__getattribute__(self, "_real")
        if isinstance(other, ShadowTorrent):
            return real == object.__getattribute__(other, "_real")
        return real == other

    def __hash__(self):
        return hash(object.__getattribute__(self, "_real"))

    def __repr__(self):
        return f"ShadowTorrent({self._real!r})"

    # ---- local state mirroring ---------------------------------------------

    def _mirror(self, key, value):
        """Update a field on the wrapped torrent without touching the API."""
        real = object.__getattribute__(self, "_real")
        try:
            real[key] = value
        except Exception:
            try:
                setattr(real, key, value)
            except Exception:
                pass

    def _current_tags(self) -> list[str]:
        real = object.__getattribute__(self, "_real")
        raw = None
        try:
            raw = real["tags"]
        except Exception:
            raw = getattr(real, "tags", "")
        return parse_tags(raw)

    # ---- mutators (record + mirror, never call the network) ----------------

    def add_tags(self, tags=None, **kwargs):
        new_tags = parse_tags(tags)
        pre = torrent_snapshot(self)
        current = self._current_tags()
        merged = current + [t for t in new_tags if t not in current]
        self._mirror("tags", tags_to_string(merged))
        self._capture.record_primitive(
            self, "add_tags", {"tags": new_tags}, f"Add tags: {', '.join(new_tags)}", watched=pre
        )

    def remove_tags(self, tags=None, **kwargs):
        remove = parse_tags(tags)
        pre = torrent_snapshot(self)
        remaining = [t for t in self._current_tags() if t not in remove]
        self._mirror("tags", tags_to_string(remaining))
        self._capture.record_primitive(
            self, "remove_tags", {"tags": remove}, f"Remove tags: {', '.join(remove)}", watched=pre
        )

    def set_category(self, category=None, **kwargs):
        pre = torrent_snapshot(self)
        self._mirror("category", category)
        self._capture.record_primitive(
            self,
            "set_category",
            {"category": category, "save_path": self.get("save_path")},
            f"Set category: {category}",
            watched=pre,
        )

    def set_auto_management(self, enable=None, **kwargs):
        pre = torrent_snapshot(self)
        self._mirror("auto_tmm", bool(enable))
        self._capture.record_primitive(
            self,
            "set_auto_management",
            {"enable": bool(enable)},
            "Enable automatic torrent management",
            watched=pre,
        )

    def set_share_limits(
        self,
        ratio_limit=None,
        seeding_time_limit=None,
        inactive_seeding_time_limit=-2,
        share_limit_action=None,
        **kwargs,
    ):
        pre = torrent_snapshot(self)
        self._mirror("ratio_limit", ratio_limit)
        self._mirror("seeding_time_limit", seeding_time_limit)
        self._mirror("inactive_seeding_time_limit", inactive_seeding_time_limit)
        self._capture.record_primitive(
            self,
            "set_share_limits",
            {
                "ratio_limit": ratio_limit,
                "seeding_time_limit": seeding_time_limit,
                "inactive_seeding_time_limit": inactive_seeding_time_limit,
                "share_limit_action": share_limit_action,
            },
            f"Set share limits: ratio={ratio_limit}, seed time={seeding_time_limit}",
            watched=pre,
        )

    def set_upload_limit(self, limit=None, **kwargs):
        pre = torrent_snapshot(self)
        self._mirror("up_limit", 0 if limit == -1 else limit)
        display = "unlimited" if limit == -1 else f"{round((limit or 0) / 1024)} kB/s"
        self._capture.record_primitive(
            self, "set_upload_limit", {"limit": limit}, f"Set upload limit: {display}", watched=pre
        )

    def resume(self, **kwargs):
        pre = torrent_snapshot(self)
        state = self.get("state", "")
        if isinstance(state, str) and state.startswith(("paused", "error")):
            self._mirror("state", "uploading")
        self._capture.record_primitive(self, "resume", {}, "Resume torrent", watched=pre)

    def pause(self, **kwargs):
        pre = torrent_snapshot(self)
        self._mirror("state", "pausedUP")
        self._capture.record_primitive(self, "pause", {}, "Pause torrent", watched=pre)

    def recheck(self, **kwargs):
        pre = torrent_snapshot(self)
        self._capture.record_primitive(self, "recheck", {}, "Force recheck torrent", watched=pre)

    def delete(self, delete_files=False, **kwargs):
        # Reached only if some code path bypasses Qbt.tor_delete_recycle.
        pre = torrent_snapshot(self)
        self._capture.record_primitive(
            self,
            "delete",
            {"delete_files": bool(delete_files)},
            f"Delete torrent (files={bool(delete_files)})",
            watched=pre,
        )


def is_shadow(obj: Any) -> bool:
    return isinstance(obj, ShadowTorrent)


def unwrap(obj: Any) -> Any:
    return object.__getattribute__(obj, "_real") if is_shadow(obj) else obj
