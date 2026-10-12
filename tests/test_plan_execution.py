"""Tests for persisted execution plans: capture, validation, execution, resume."""

from __future__ import annotations

import hashlib
import os
from types import SimpleNamespace

import pytest

from modules.plan import service
from modules.plan.capture import PlanCapture
from modules.plan.executor import PlanExecutor
from modules.plan.models import ActionResult
from modules.plan.models import ActionType
from modules.plan.models import empty_summary
from modules.plan.snapshot import stat_snapshot
from modules.plan.snapshot import torrent_snapshot
from modules.plan.store import PlanStore
from modules.plan.validator import PlanValidator
from tests.factories import FakeClient
from tests.factories import FakeConfig
from tests.factories import FakeQbtManager
from tests.factories import FakeTorrent

HASH = "0" * 40
HASH2 = "1" * 40
NOW = "2026-01-01T00:00:00Z"


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text("qbt:\n  host: http://example\n")
    return path


def make_qbt(torrents=None, config=None, client=None):
    config = config or FakeConfig()
    qbt = FakeQbtManager(torrents=torrents or [], config=config, client=client or FakeClient())
    config.qbt = qbt
    qbt.host = "http://example"
    qbt.username = None
    return config, qbt


def make_action(
    seq,
    type_,
    torrent=None,
    *,
    command="tag_update",
    params=None,
    reason="test",
    modifies=True,
    deletes=False,
    files=None,
    watched=None,
    target=None,
):
    return {
        "id": f"a{seq + 1}",
        "seq": seq,
        "type": type_,
        "command": command,
        "target": target or ({"hash": torrent.hash, "name": torrent.name} if torrent else {}),
        "reason": reason,
        "params": params or {},
        "modifies": modifies,
        "deletes": deletes,
        "watched": watched if watched is not None else (torrent_snapshot(torrent) if torrent else {}),
        "files": files or [],
        "result": ActionResult.PENDING,
        "message": None,
        "checked_at": None,
        "finished_at": None,
    }


def make_plan(actions, tmp_path, config_text="qbt:\n  host: http://example\n", **overrides):
    plan = {
        "plan_id": "plan123",
        "schema_version": 1,
        "config_file": "config.yml",
        "config_text": config_text,
        "config_sha256": hashlib.sha256(config_text.encode()).hexdigest(),
        "qbt": {"host": "http://example", "username": None},
        "path": {"root_dir": str(tmp_path), "remote_dir": str(tmp_path)},
        "commands": ["tag_update"],
        "hashes": [],
        "created_at": NOW,
        "updated_at": NOW,
        "heartbeat_at": None,
        "status": "pending",
        "summary": empty_summary(),
        "actions": actions,
    }
    plan.update(overrides)
    return plan


# ---- capture ---------------------------------------------------------------


def test_capture_records_tag_action_and_mirrors_state(config_file):
    config, qbt = make_qbt([FakeTorrent(hash=HASH, tags="")])
    config.config_path = str(config_file)
    config.dry_run = True
    qbt.get_tags = lambda urls: {"tag": ["tracker1"], "cat": "", "notifiarr": None, "url": urls[0] if urls else ""}

    capture = PlanCapture(config)
    config.plan = capture
    capture.install()
    try:
        assert config.dry_run is False
        Tags = __import__("modules.core.tags", fromlist=["Tags"]).Tags
        Tags(qbt)
        plan = capture.finalize(["tag_update"], [])
    finally:
        capture.restore()
        config.plan = None

    assert config.dry_run is True
    tag_actions = [a for a in plan["actions"] if a["type"] == ActionType.TAGS_ADD]
    assert len(tag_actions) == 1
    action = tag_actions[0]
    assert action["params"]["tags"] == ["tracker1"]
    assert action["target"]["hash"] == HASH
    assert action["modifies"] is True
    # Precondition snapshot captures the PRE-mutation state (missing tag).
    assert action["watched"]["tags"] == ""
    assert "tracker1" in action["reason"]
    assert qbt._torrents[0].tags == "tracker1"  # simulated state mirror
    assert config.notifications_sent == []  # notifications silenced during preview
    assert plan["config_sha256"] == hashlib.sha256(config_file.read_bytes()).hexdigest()


def test_shadow_returns_same_instance_on_refetch(config_file):
    config, qbt = make_qbt([FakeTorrent(hash=HASH)])
    capture = PlanCapture(config)
    capture.install()
    try:
        first = qbt.get_torrents({"torrent_hashes": HASH})[0]
        second = qbt.get_torrents({"torrent_hashes": HASH})[0]
        assert first is second
        first.add_tags(["a", "b"])
        assert second.tags == "a, b"
    finally:
        capture.restore()


def test_capture_delete_action_records_attr_and_destructive_flag(config_file):
    torrent = FakeTorrent(hash=HASH, tags="", content_path="/data/t/x", save_path="/data/t")
    config, qbt = make_qbt([torrent])
    config.config_path = str(config_file)
    capture = PlanCapture(config)
    capture.install()
    try:
        shadow = qbt.get_torrents({"torrent_hashes": HASH})[0]
        attr = {
            "function": "rem_unregistered",
            "torrents": [torrent.name],
            "torrent_category": "Movies",
            "torrent_status": "Unregistered torrent",
            "torrents_deleted_and_contents": True,
        }
        qbt.tor_delete_recycle(shadow, attr)
        plan = capture.finalize(["rem_unregistered"], [])
    finally:
        capture.restore()

    actions = [a for a in plan["actions"] if a["type"] == ActionType.UNREG_DELETE]
    assert len(actions) == 1
    action = actions[0]
    assert action["deletes"] is True
    assert action["params"]["attr"]["torrents_deleted_and_contents"] is True
    assert action["watched"]["trackers"][0][0].startswith("http")


def test_capture_orphan_files_and_rmdirs(tmp_path, config_file):
    movie = tmp_path / "movie.mkv"
    movie.write_text("x")
    config = FakeConfig(root_dir=str(tmp_path), remote_dir=str(tmp_path), orphaned_dir=str(tmp_path / "orphan"))
    config.config_path = str(config_file)
    config.qbt = FakeQbtManager(torrents=[], config=config, client=FakeClient())
    capture = PlanCapture(config)
    capture.install()
    try:
        capture.record_orphan_files([str(movie)])
        plan = capture.finalize(["rem_orphaned"], [])
    finally:
        capture.restore()

    file_action = next(a for a in plan["actions"] if a["type"] == ActionType.ORPHAN_FILE_DELETE)
    rmdir_action = next(a for a in plan["actions"] if a["type"] == ActionType.ORPHAN_RMDIR)
    assert file_action["files"][0]["ino"] == movie.stat().st_ino
    assert file_action["files"][0]["nlink"] == 1
    assert file_action["deletes"] is True
    assert rmdir_action["params"]["dir"] == str(tmp_path)


def test_captured_plan_executes_on_unchanged_state(tmp_path, config_file):
    # Capture side
    torrent = FakeTorrent(hash=HASH, tags="")
    config, qbt = make_qbt([torrent])
    config.config_path = str(config_file)
    capture = PlanCapture(config)
    capture.install()
    try:
        qbt.get_torrents({"torrent_hashes": HASH})[0].add_tags(["new"])
        plan = capture.finalize(["tag_update"], [])
    finally:
        capture.restore()

    # Execution side: same initial state, fresh manager/plan document
    target_torrent = FakeTorrent(hash=HASH, tags="")
    config2, qbt2 = make_qbt([target_torrent])
    store = PlanStore(str(tmp_path))
    store.save(plan)
    result = PlanExecutor(store.load(plan["plan_id"]), qbt2, config2, store).run()
    assert plan["actions"][0]["watched"]["tags"] == ""
    assert result["summary"][ActionResult.APPLIED] == 1
    assert ("add_tags",) == tuple(call[0] for call in target_torrent.calls)
    assert target_torrent.tags == "new"


# ---- validation ------------------------------------------------------------


def test_validator_tag_unchanged_passes():
    torrent = FakeTorrent(tags="")
    config, qbt = make_qbt([torrent])
    action = make_action(0, ActionType.TAGS_ADD, torrent, params={"tags": ["x"]})
    ok, reason = PlanValidator(qbt, config).check_action(action)
    assert ok and reason is None


def test_validator_tag_change_is_stale():
    torrent = FakeTorrent(tags="")
    config, qbt = make_qbt([torrent])
    action = make_action(0, ActionType.TAGS_ADD, torrent, params={"tags": ["x"]})
    qbt._torrents[0].tags = "someone_else_was_here"
    ok, reason = PlanValidator(qbt, config).check_action(action)
    assert not ok and "tags" in reason


def test_validator_missing_torrent_is_stale():
    torrent = FakeTorrent()
    config, qbt = make_qbt([])
    action = make_action(0, ActionType.TAGS_REMOVE, torrent, params={"tags": ["x"]})
    ok, reason = PlanValidator(qbt, config).check_action(action)
    assert not ok and "no longer exists" in reason


def test_validator_orphan_re_referenced_is_stale(tmp_path):
    movie = tmp_path / "movie.mkv"
    movie.write_text("x")
    referenced = FakeTorrent(save_path=str(tmp_path), files=[SimpleNamespace(name="movie.mkv")])
    config, qbt = make_qbt([referenced], config=FakeConfig(root_dir=str(tmp_path), remote_dir=str(tmp_path)))
    snap = stat_snapshot(str(movie), str(movie))
    action = make_action(
        0,
        ActionType.ORPHAN_FILE_DELETE,
        None,
        command="rem_orphaned",
        params={"src": str(movie), "dest": str(tmp_path / "orphan" / "movie.mkv")},
        files=[snap],
    )
    ok, reason = PlanValidator(qbt, config).check_action(action)
    assert not ok and "referenced" in reason


def test_validator_orphan_identity_change_is_stale(tmp_path):
    movie = tmp_path / "movie.mkv"
    movie.write_text("x")
    config, qbt = make_qbt([], config=FakeConfig(root_dir=str(tmp_path), remote_dir=str(tmp_path)))
    snap = stat_snapshot(str(movie), str(movie))
    movie.write_text("x" * 1000)  # size/inode identity changes
    action = make_action(
        0,
        ActionType.ORPHAN_FILE_DELETE,
        None,
        command="rem_orphaned",
        params={"src": str(movie)},
        files=[snap],
    )
    ok, reason = PlanValidator(qbt, config).check_action(action)
    assert not ok and "identity" in reason


def test_validator_nohl_nlink_change_is_stale(tmp_path):
    movie = tmp_path / "movie.mkv"
    movie.write_text("x")
    torrent = FakeTorrent(tags="", content_path=str(movie), save_path=str(tmp_path))
    config, qbt = make_qbt([torrent], config=FakeConfig(root_dir=str(tmp_path), remote_dir=str(tmp_path)))
    snap = stat_snapshot(str(movie), str(movie))
    os.link(movie, tmp_path / "hardlink.mkv")
    action = make_action(0, ActionType.NOHL_ADD, torrent, files=[snap])
    ok, reason = PlanValidator(qbt, config).check_action(action)
    assert not ok and "Hard link" in reason


def test_validator_rmdir_nonempty_is_stale(tmp_path):
    directory = tmp_path / "empty_dir"
    directory.mkdir()
    (directory / "file").write_text("x")
    config, qbt = make_qbt([], config=FakeConfig(root_dir=str(tmp_path), remote_dir=str(tmp_path)))
    action = make_action(0, ActionType.ORPHAN_RMDIR, None, command="rem_orphaned", params={"dir": str(directory)})
    ok, reason = PlanValidator(qbt, config).check_action(action)
    assert not ok and "no longer empty" in reason


def test_plan_level_config_drift(tmp_path, config_file):
    config, qbt = make_qbt([])
    config.config_path = str(config_file)
    config.root_dir = str(tmp_path)
    config.remote_dir = str(tmp_path)
    plan = make_plan([], tmp_path)
    assert PlanValidator(qbt, config).plan_level(plan) == []
    config_file.write_text("qbt:\n  host: http://changed\n")
    reasons = PlanValidator(qbt, config).plan_level(plan)
    assert any("changed" in r for r in reasons)


# ---- execution -------------------------------------------------------------


def test_executor_applies_unchanged_tag_action(tmp_path):
    torrent = FakeTorrent(tags="")
    config, qbt = make_qbt([torrent])
    action = make_action(0, ActionType.TAGS_ADD, torrent, params={"tags": ["new"]})
    plan = make_plan([action], tmp_path)
    store = PlanStore(str(tmp_path))
    result = PlanExecutor(plan, qbt, config, store).run()
    assert action["result"] == ActionResult.APPLIED
    assert result["status"] == "completed"
    assert result["summary"][ActionResult.APPLIED] == 1
    assert ("add_tags",) == tuple(call[0] for call in torrent.calls)
    on_disk = store.load("plan123")
    assert on_disk["actions"][0]["result"] == ActionResult.APPLIED


def test_executor_stale_action_has_no_side_effects(tmp_path):
    torrent = FakeTorrent(tags="")
    config, qbt = make_qbt([torrent])
    action = make_action(0, ActionType.TAGS_ADD, torrent, params={"tags": ["new"]})
    qbt._torrents[0].tags = "changed"
    plan = make_plan([action], tmp_path)
    PlanExecutor(plan, qbt, config, PlanStore(str(tmp_path))).run()
    assert action["result"] == ActionResult.STALE
    assert torrent.calls == []


def test_executor_failed_postcheck_marked_failed(tmp_path):
    torrent = FakeTorrent(tags="")
    torrent.add_tags = lambda tags: None  # API "succeeds" but state does not stick
    config, qbt = make_qbt([torrent])
    action = make_action(0, ActionType.TAGS_ADD, None, params={"tags": ["new"]}, watched={"tags": ""})
    plan = make_plan([action], tmp_path)
    result = PlanExecutor(plan, qbt, config, PlanStore(str(tmp_path))).run()
    assert action["result"] == ActionResult.FAILED
    assert result["status"] == "completed_with_failures"


def test_executor_orphan_delete_and_move(tmp_path):
    movie = tmp_path / "movie.mkv"
    movie.write_text("x")
    dest_dir = tmp_path / "orphan"
    config = FakeConfig(root_dir=str(tmp_path), remote_dir=str(tmp_path), orphaned_dir=str(dest_dir))
    _, qbt = make_qbt([], config=config)

    delete_action = make_action(
        0,
        ActionType.ORPHAN_FILE_DELETE,
        None,
        command="rem_orphaned",
        params={"src": str(movie)},
        files=[stat_snapshot(str(movie), str(movie))],
    )
    PlanExecutor(make_plan([delete_action], tmp_path), qbt, config, PlanStore(str(tmp_path))).run()
    assert delete_action["result"] == ActionResult.APPLIED
    assert not movie.exists()

    movie2 = tmp_path / "movie2.mkv"
    movie2.write_text("y")
    move_action = make_action(
        1,
        ActionType.ORPHAN_FILE_MOVE,
        None,
        command="rem_orphaned",
        params={"src": str(movie2), "dest": str(dest_dir / "movie2.mkv")},
        files=[stat_snapshot(str(movie2), str(movie2))],
    )
    plan = make_plan([move_action], tmp_path)
    PlanExecutor(plan, qbt, config, PlanStore(str(tmp_path))).run()
    assert move_action["result"] == ActionResult.APPLIED
    assert not movie2.exists() and (dest_dir / "movie2.mkv").exists()


def test_executor_delete_reconcile_states(tmp_path):
    content = tmp_path / "content.mkv"
    content.write_text("data")
    attr = {"function": "rem_unregistered", "torrents": ["T"], "torrents_deleted_and_contents": True}

    # API call succeeds but torrent still there -> failed
    torrent = FakeTorrent(save_path=str(tmp_path), content_path=str(content))
    config, qbt = make_qbt([torrent])
    action = make_action(
        0, ActionType.UNREG_DELETE, torrent, params={"attr": attr}, files=[stat_snapshot(str(content), str(content))]
    )
    PlanExecutor(make_plan([action], tmp_path), qbt, config, PlanStore(str(tmp_path))).run()
    assert action["result"] == ActionResult.FAILED

    # API removes the torrent but not the content -> uncertain
    qbt._torrents = [torrent]
    qbt.tor_delete_recycle = lambda t, a: qbt._torrents.clear()
    action2 = make_action(
        1, ActionType.UNREG_DELETE, torrent, params={"attr": attr}, files=[stat_snapshot(str(content), str(content))]
    )
    plan = make_plan([action2], tmp_path)
    PlanExecutor(plan, qbt, config, PlanStore(str(tmp_path))).run()
    assert action2["result"] == ActionResult.UNCERTAIN

    # Torrent removed and content gone -> applied
    action3 = make_action(
        2, ActionType.UNREG_DELETE, torrent, params={"attr": attr}, files=[stat_snapshot(str(content), str(content))]
    )

    def delete_and_remove(t, a):
        qbt.tor_delete_recycle_calls.append((t, a))
        qbt._torrents = []
        content.unlink()

    qbt._torrents = [torrent]
    qbt.tor_delete_recycle = delete_and_remove
    plan = make_plan([action3], tmp_path)
    PlanExecutor(plan, qbt, config, PlanStore(str(tmp_path))).run()
    assert action3["result"] == ActionResult.APPLIED


def test_executor_cross_seed_recomputed_to_keep_content(tmp_path):
    content = tmp_path / "content.mkv"
    content.write_text("data")
    torrent = FakeTorrent(save_path=str(tmp_path), content_path=str(content))
    config = FakeConfig(root_dir=str(tmp_path), remote_dir=str(tmp_path))
    _, qbt = make_qbt([torrent], config=config)
    qbt.has_cross_seed = lambda t: True
    attr = {"function": "cleanup_share_limits", "torrents": ["T"], "torrents_deleted_and_contents": True}
    action = make_action(
        0,
        ActionType.SHARE_CLEANUP_DELETE,
        torrent,
        command="share_limits",
        params={"attr": attr},
        files=[stat_snapshot(str(content), str(content))],
    )

    def delete_call(t, a):
        assert a["torrents_deleted_and_contents"] is False
        qbt._torrents = []

    qbt.tor_delete_recycle = delete_call
    plan = make_plan([action], tmp_path)
    PlanExecutor(plan, qbt, config, PlanStore(str(tmp_path))).run()
    assert action["result"] == ActionResult.APPLIED


def test_resume_skips_terminal_actions_and_keeps_results(tmp_path):
    t1, t2 = FakeTorrent(hash=HASH, tags=""), FakeTorrent(hash=HASH2, tags="")
    config, qbt = make_qbt([t1, t2])
    a1 = make_action(0, ActionType.TAGS_ADD, t1, params={"tags": ["one"]})
    a2 = make_action(1, ActionType.TAGS_ADD, t2, params={"tags": ["two"]})
    a1["result"] = ActionResult.APPLIED  # simulate previous partial run persisted on disk
    plan = make_plan([a1, a2], tmp_path, status="running")
    PlanExecutor(plan, qbt, config, PlanStore(str(tmp_path))).run()
    assert a1["result"] == ActionResult.APPLIED
    assert all(call[0] != "add_tags" for call in t1.calls)
    assert a2["result"] == ActionResult.APPLIED
    assert ("add_tags",) == tuple(call[0] for call in t2.calls)


def test_skip_ids_mark_actions_skipped(tmp_path):
    torrent = FakeTorrent(tags="")
    config, qbt = make_qbt([torrent])
    action = make_action(0, ActionType.TAGS_ADD, torrent, params={"tags": ["x"]})
    plan = make_plan([action], tmp_path)
    PlanExecutor(plan, qbt, config, PlanStore(str(tmp_path))).run(skip_ids={"a1"})
    assert action["result"] == ActionResult.SKIPPED
    assert torrent.calls == []


def test_summary_notification_uses_definite_results_only(tmp_path):
    sent = []
    torrent = FakeTorrent(tags="")
    config, qbt = make_qbt([torrent])
    config.webhooks_factory.end_time_hooks = lambda *a, **k: sent.append((a, k))
    action = make_action(0, ActionType.TAGS_ADD, None, watched={"tags": ""}, params={"tags": ["x"]})
    # action will fail (no live torrent tag state after add no-ops) -- force an applied action instead
    plan = make_plan([action], tmp_path)
    PlanExecutor(plan, qbt, config, PlanStore(str(tmp_path))).run()
    assert len(sent) == 1
    body = sent[0][0][5]
    assert "Applied: 0" in body and "Failed: 1" in body


# ---- store / service -------------------------------------------------------


def test_store_roundtrip_and_skip_action(tmp_path):
    store = PlanStore(str(tmp_path))
    torrent = FakeTorrent(tags="")
    plan = make_plan([make_action(0, ActionType.TAGS_ADD, torrent, params={"tags": ["x"]})], tmp_path)
    store.save(plan)
    assert store.list_plans()[0]["action_count"] == 1

    updated = service.skip_action(str(tmp_path), "plan123", "a1")
    assert updated["actions"][0]["result"] == ActionResult.SKIPPED
    with pytest.raises(ValueError):
        service.skip_action(str(tmp_path), "plan123", "a1")
    with pytest.raises(FileNotFoundError):
        service.skip_action(str(tmp_path), "plan123", "missing")


def test_cheap_plan_validity_detects_changed_config(tmp_path, config_file):
    plan = make_plan([], tmp_path)
    assert service.cheap_plan_validity(str(tmp_path), plan) == []
    config_file.write_text("qbt:\n  host: http://changed\n")
    assert service.cheap_plan_validity(str(tmp_path), plan)
