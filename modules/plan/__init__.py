"""Persistent execution plans generated from a Web UI preview run.

A plan freezes the configuration, command selection, torrent scope and the
qBittorrent/filesystem state every proposed action depends on. Confirmation
re-validates each action's preconditions before performing it and persists
every outcome so interrupted runs can resume without re-deciding anything.
"""

from modules.plan.capture import PlanCapture
from modules.plan.models import ActionResult
from modules.plan.models import ActionType
from modules.plan.models import PlanStatus
from modules.plan.service import apply_plan
from modules.plan.service import check_plan
from modules.plan.service import create_plan_for_config
from modules.plan.store import PlanStore

__all__ = [
    "ActionResult",
    "ActionType",
    "PlanCapture",
    "PlanStatus",
    "PlanStore",
    "apply_plan",
    "check_plan",
    "create_plan_for_config",
]
