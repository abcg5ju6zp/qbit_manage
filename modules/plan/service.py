"""Application service orchestrating plan creation, validation and execution."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime
from typing import Any

from modules import util
from modules.config import Config
from modules.plan.capture import PlanCapture
from modules.plan.executor import PlanExecutor
from modules.plan.models import ActionResult
from modules.plan.store import PlanStore
from modules.plan.validator import PlanValidator
from modules.util import execute_qbit_commands

logger = util.logger

COMMAND_FLAGS = [
    "recheck",
    "cat_update",
    "tag_update",
    "rem_unregistered",
    "tag_tracker_error",
    "rem_orphaned",
    "tag_nohardlinks",
    "share_limits",
]


class PlanInvalid(Exception):
    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


def build_run_args(
    web_args: dict[str, Any],
    *,
    config_file: str,
    commands: list[str],
    hashes: list[str] | None,
    dry_run: bool,
    skip_cleanup: bool = False,
    skip_qb_version_check: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the args dict consumed by Config/execute_qbit_commands."""
    now = now or datetime.now()
    run_args = dict(web_args)
    run_args.update(
        {
            "_from_web_api": True,
            "dry_run": dry_run,
            "time": now.strftime("%H:%M"),
            "time_obj": now,
            "run": True,
            "hashes": hashes or [],
            "config_file": config_file,
            "config_files": [config_file],
        }
    )
    for flag in COMMAND_FLAGS + ["skip_cleanup", "skip_qb_version_check"]:
        run_args[flag] = False
    for cmd in commands:
        if cmd not in run_args:
            raise ValueError(f"Invalid command: {cmd}")
        run_args[cmd] = True
    run_args["skip_cleanup"] = skip_cleanup
    run_args["skip_qb_version_check"] = skip_qb_version_check
    return run_args


def _construction_args(run_args: dict[str, Any], config_file: str) -> dict[str, Any]:
    """Args for building a Config that must not perform init-time writes.

    All command flags are forced off and dry_run on, which suppresses the
    force_auto_tmm / default-share-limit writes in Qbt.__init__.
    """
    args = dict(run_args)
    args.update({"config_file": config_file, "config_files": [config_file], "dry_run": True})
    for flag in COMMAND_FLAGS:
        args[flag] = False
    return args


def create_plan_for_config(
    default_dir: str,
    run_args: dict[str, Any],
    config_file: str,
    commands: list[str],
    hashes: list[str] | None,
) -> dict[str, Any]:
    store = PlanStore(default_dir)
    store.prune()
    build_args = _construction_args(run_args, config_file)
    cfg = Config(default_dir, build_args)

    capture = PlanCapture(cfg, store)
    cfg.plan = capture
    config_base = os.path.splitext(config_file)[0]
    logger.add_config_handler(config_base)
    try:
        capture.install()
        # Enable the requested commands on the already-constructed Config so
        # execute_qbit_commands runs them while shadows intercept every write.
        for command in commands:
            cfg.commands[command] = True
        stats = {"executed_commands": []}
        execute_qbit_commands(cfg.qbt, cfg.commands, stats, hashes=hashes)
        plan = capture.finalize(commands, hashes)
        logger.info(f"Execution plan {plan['plan_id']} captured with {len(plan['actions'])} action(s)")
        return plan
    finally:
        capture.restore()
        cfg.plan = None
        logger.remove_config_handler(config_base)


def _build_config(default_dir: str, run_args: dict[str, Any], config_file: str) -> Config:
    cfg = Config(default_dir, _construction_args(run_args, config_file))
    # Populate torrentfiles/torrentinfo (cross-seed index, category paths)
    # without init-time writes; dry_run stays True.
    try:
        cfg.qbt.get_torrent_info()
    except Exception as e:
        logger.warning(f"Plan apply: rebuilding torrent info failed: {e}")
    return cfg


def check_plan(
    default_dir: str,
    run_args: dict[str, Any],
    plan: dict[str, Any],
    *,
    connect: bool,
) -> dict[str, Any]:
    """Return the plan enriched with validity information (does not persist)."""
    enriched = dict(plan)
    if not connect:
        reasons = cheap_plan_validity(default_dir, plan)
        enriched["valid"] = not reasons
        enriched["invalid_reasons"] = reasons
        return enriched

    cfg = _build_config(default_dir, run_args, plan["config_file"])
    validator = PlanValidator(cfg.qbt, cfg)
    reasons = validator.plan_level(plan)
    enriched["valid"] = not reasons
    enriched["invalid_reasons"] = reasons
    if not reasons:
        for action in enriched["actions"]:
            if action.get("result") != ActionResult.PENDING:
                action["preflight"] = {"valid": True, "reason": None, "terminal": True}
                continue
            ok, reason = validator.check_action(action)
            action["preflight"] = {"valid": ok, "reason": reason, "terminal": False}
    return enriched


def cheap_plan_validity(default_dir: str, plan: dict[str, Any]) -> list[str]:
    config_path = os.path.join(default_dir, plan["config_file"])
    if not os.path.isfile(config_path):
        return [f"Config file no longer exists: {plan['config_file']}"]
    try:
        with open(config_path, "rb") as fh:
            current_sha = hashlib.sha256(fh.read()).hexdigest()
    except OSError as e:
        return [f"Config file cannot be read: {e}"]
    if current_sha != plan.get("config_sha256"):
        return ["Config file content changed since the plan was created"]
    return []


def apply_plan(
    default_dir: str,
    run_args: dict[str, Any],
    plan: dict[str, Any],
    skip_ids: set[str] | None = None,
) -> dict[str, Any]:
    store = PlanStore(default_dir)
    cfg = _build_config(default_dir, run_args, plan["config_file"])
    validator = PlanValidator(cfg.qbt, cfg)
    reasons = validator.plan_level(plan)
    if reasons:
        plan["status"] = "invalid"
        plan["updated_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        store.save(plan)
        raise PlanInvalid(reasons)
    config_base = os.path.splitext(plan["config_file"])[0]
    logger.add_config_handler(config_base)
    try:
        executor = PlanExecutor(plan, cfg.qbt, cfg, store)
        return executor.run(skip_ids=skip_ids)
    finally:
        logger.remove_config_handler(config_base)


def skip_action(default_dir: str, plan_id: str, action_id: str) -> dict[str, Any]:
    store = PlanStore(default_dir)
    plan = store.load(plan_id)
    if plan is None:
        raise FileNotFoundError(plan_id)
    if plan["status"] == "invalid":
        raise PlanInvalid(["Plan is invalid and cannot be modified"])
    for action in plan["actions"]:
        if action.get("id") == action_id:
            if action.get("result") != ActionResult.PENDING:
                raise ValueError(f"Action {action_id} is already {action.get('result')}")
            action["result"] = ActionResult.SKIPPED
            action["message"] = "Skipped by operator"
            action["finished_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            store.save(plan)
            return plan
    raise FileNotFoundError(action_id)
