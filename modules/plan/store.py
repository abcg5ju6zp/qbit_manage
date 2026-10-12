"""Filesystem-backed plan storage: one atomic JSON document per plan."""

from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Any

from modules import util
from modules.plan.models import PLAN_RETENTION_DAYS

logger = util.logger


class PlanStore:
    def __init__(self, config_dir: str):
        self.dir = os.path.join(config_dir, "plans")
        os.makedirs(self.dir, exist_ok=True)

    def path(self, plan_id: str) -> str:
        return os.path.join(self.dir, f"{plan_id}.json")

    def exists(self, plan_id: str) -> bool:
        return os.path.isfile(self.path(plan_id))

    def save(self, plan: dict[str, Any]) -> None:
        """Atomically persist a plan document (tmp file + os.replace)."""
        dest = self.path(plan["plan_id"])
        directory = os.path.dirname(dest)
        fd, tmp_path = tempfile.mkstemp(prefix=".plan-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(plan, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, dest)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def load(self, plan_id: str) -> dict[str, Any] | None:
        path = self.path(plan_id)
        if not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def delete(self, plan_id: str) -> bool:
        path = self.path(plan_id)
        if os.path.isfile(path):
            os.unlink(path)
            return True
        return False

    def list_plans(self) -> list[dict[str, Any]]:
        plans = []
        if not os.path.isdir(self.dir):
            return plans
        for name in os.listdir(self.dir):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.dir, name), encoding="utf-8") as fh:
                    plan = json.load(fh)
                plans.append(self.summary(plan))
            except (OSError, json.JSONDecodeError) as e:
                logger.warning(f"Could not read plan file {name}: {e}")
        plans.sort(key=lambda p: p.get("created_at", ""), reverse=True)
        return plans

    @staticmethod
    def summary(plan: dict[str, Any]) -> dict[str, Any]:
        actions = plan.get("actions", [])
        counts: dict[str, int] = {}
        for action in actions:
            counts[action.get("result", "pending")] = counts.get(action.get("result", "pending"), 0) + 1
        return {
            "plan_id": plan["plan_id"],
            "config_file": plan.get("config_file"),
            "commands": plan.get("commands", []),
            "hashes": plan.get("hashes", []),
            "status": plan.get("status"),
            "created_at": plan.get("created_at"),
            "updated_at": plan.get("updated_at"),
            "heartbeat_at": plan.get("heartbeat_at"),
            "action_count": len(actions),
            "result_counts": counts,
        }

    def prune(self, days: int = PLAN_RETENTION_DAYS) -> None:
        """Delete terminal plan documents older than the retention window."""
        from modules.plan.models import ActionResult
        from modules.plan.models import PlanStatus

        cutoff = time.time() - days * 86400
        for name in os.listdir(self.dir) if os.path.isdir(self.dir) else []:
            if not name.endswith(".json"):
                continue
            path = os.path.join(self.dir, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    plan = json.load(fh)
                if plan.get("status") not in {PlanStatus.COMPLETED, PlanStatus.COMPLETED_WITH_FAILURES, PlanStatus.INVALID}:
                    continue
                if any(a.get("result") == ActionResult.PENDING for a in plan.get("actions", [])):
                    continue
                if os.path.getmtime(path) < cutoff:
                    os.unlink(path)
                    logger.debug(f"Pruned old execution plan {name}")
            except (OSError, json.JSONDecodeError) as e:
                logger.warning(f"Could not prune plan file {name}: {e}")
