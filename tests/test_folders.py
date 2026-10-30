"""Folder inspector, flatten/undo, and interruption safety on real filesystems."""
from dataclasses import replace
import errno
import json
import os
from pathlib import Path
import sqlite3
import threading

import pytest

from media_tool.folders import folder_view, scan_folder
from media_tool.folder_moves import (cleanup_rows, plan_flatten, plan_folder_undo,
                                     projected_snapshot)
from media_tool.jobs import Cancelled, SCHEMA, Store, execute_job


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "history")
    yield result
    result.close()


def tree_fixture(tmp_path):
    root = tmp_path / "root"
    for name, content in (("ParentA/Child2/a 日本.txt", b"abc"),
                          ("ParentA/Child10/deeper/b.bin", b"1234567"),
                          ("ParentB/Other/c.txt", b"12345"),
                          ("ParentA/loose.txt", b"xy"), ("root.txt", b"z")):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (root / "empty").mkdir()
    return root


def files(root):
    return {p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file()}


def flatten(store, root, **options):
    plan = plan_flatten(scan_folder(root), **options)
    assert plan.can_apply
    return store.enqueue("Flatten", "flatten", plan.specs), plan


def test_cached_totals_filters_depth_and_natural_order(tmp_path, monkeypatch):
    root = tree_fixture(tmp_path)
    snapshot = scan_folder(root)
    assert snapshot.complete
    assert snapshot.nodes[0].total == 18
    assert snapshot.nodes[0].files == 5
    monkeypatch.setattr(os, "scandir", lambda *args: pytest.fail("Cached filtering must not rescan"))
    for depth in (0, 1, 2, 3):
        view = folder_view(snapshot, minimum=3, maximum=6, depth=depth, directories_only=True)
        assert view.matching_bytes[0] == 8
        assert view.matching_files[0] == 2
        assert all(snapshot.nodes[c].kind == "Folder" for chosen in view.children for c in chosen)
    view = folder_view(snapshot)
    parent = next(i for i, n in enumerate(snapshot.nodes) if n.name == "ParentA")
    assert [snapshot.nodes[i].name for i in view.children[parent]] == ["Child2", "Child10", "loose.txt"]
    view = folder_view(snapshot, sort="size")
    assert snapshot.nodes[view.children[0][0]].name == "ParentA"


@pytest.mark.parametrize("minimum,maximum", [(-1, None), (10, 9)])
def test_invalid_size_ranges(tmp_path, minimum, maximum):
    with pytest.raises(ValueError):
        folder_view(scan_folder(tree_fixture(tmp_path)), minimum, maximum)


def test_empty_root(tmp_path):
    snapshot = scan_folder(tmp_path)
    assert snapshot.complete and snapshot.nodes[0].total == 0
    assert not plan_flatten(snapshot).can_apply


def test_deep_tree_and_large_directory(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    path = root
    for _ in range(45):
        path = path / "d"
        path.mkdir()
    (path / "a").write_bytes(b"deep")
    for i in range(520):
        (root / f"file{i}.txt").write_bytes(b"x")
    snapshot = scan_folder(root)
    assert snapshot.complete and snapshot.nodes[0].total == 524
    assert snapshot.nodes[0].files == 521
    assert folder_view(snapshot, depth=1).matching_bytes[0] == 524


def test_denied_scan_labels_partial_and_blocks_moves(tmp_path, monkeypatch):
    root = tree_fixture(tmp_path)
    original = os.scandir
    def denied(path):
        if Path(path).name == "Child2":
            raise PermissionError("simulated unreadable folder")
        return original(path)
    monkeypatch.setattr(os, "scandir", denied)
    snapshot = scan_folder(root)
    assert not snapshot.complete and snapshot.nodes[0].partial
    assert any("unreadable" in issue for issue in snapshot.issues)
    with pytest.raises(ValueError, match="complete"):
        plan_flatten(snapshot)


def test_cancelled_scan_retains_partial_snapshot(tmp_path):
    root = tree_fixture(tmp_path)
    calls = 0
    def stop():
        nonlocal calls
        calls += 1
        return calls > 3
    snapshot = scan_folder(root, stop)
    assert snapshot.cancelled and not snapshot.complete
    assert "partial" in snapshot.nodes[0].issue
    assert folder_view(snapshot).snapshot.nodes[0].partial


def test_scan_records_directory_changed_mid_scan(tmp_path, monkeypatch):
    root = tree_fixture(tmp_path)
    original = os.scandir
    class ChangingEntries:
        def __init__(self, path):
            self.path = Path(path)
            self.entries = original(path)
        def __enter__(self):
            return self.entries.__enter__()
        def __exit__(self, *args):
            result = self.entries.__exit__(*args)
            if self.path == root:
                (root / "new.txt").write_bytes(b"arrived")
            return result
    monkeypatch.setattr(os, "scandir", ChangingEntries)
    snapshot = scan_folder(root)
    assert not snapshot.complete
    assert any("changed during discovery" in i for i in snapshot.issues)


def test_scan_rechecks_earlier_directories_after_later_subtrees(tmp_path, monkeypatch):
    root = tree_fixture(tmp_path)
    original = os.scandir
    class LaterChange:
        def __init__(self, path):
            self.path, self.entries = Path(path), original(path)
        def __enter__(self):
            return self.entries.__enter__()
        def __exit__(self, *args):
            result = self.entries.__exit__(*args)
            if self.path.name == "Child2":
                (root / "arrived_later").write_bytes(b"new")
            return result
    monkeypatch.setattr(os, "scandir", LaterChange)
    snapshot = scan_folder(root)
    assert not snapshot.complete
    assert any("before the scan finished" in issue for issue in snapshot.issues)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction-specific check")
def test_windows_junction_is_not_traversed_or_moved(store, tmp_path):
    import _winapi
    root = tree_fixture(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    (external / "keep.txt").write_bytes(b"outside")
    junction = root / "ParentA" / "Child2" / "junction"
    alias = root / "ParentA" / "alias"
    _winapi.CreateJunction(str(external), str(junction))
    _winapi.CreateJunction(str(root / "ParentB" / "Other"), str(alias))
    try:
        snapshot = scan_folder(root)
        assert snapshot.nodes[0].total == 18
        assert any(n.kind == "Link" and n.path == str(junction) for n in snapshot.nodes)
        plan = plan_flatten(snapshot)
        assert any(i.status == "Skipped" and i.source == str(junction.parent) for i in plan.items)
        job = store.enqueue("Skip linked subtree", "flatten", plan.specs)
        assert execute_job(store, job) in ("succeeded", "completed with skips")
        assert junction.is_dir() and junction.parent.is_dir()
        assert (external / "keep.txt").read_bytes() == b"outside"
        assert (root / "Other" / "c.txt").read_bytes() == b"12345"
        with pytest.raises(ValueError, match="Links"):
            scan_folder(junction)
    finally:
        junction.rmdir()  # Remove this named junction, never its target.
        alias.rmdir()


def test_links_are_visible_and_never_followed(tmp_path):
    root = tree_fixture(tmp_path)
    target = tmp_path / "external"
    target.mkdir()
    (target / "secret").write_bytes(b"outside")
    link = root / "ParentA" / "Child2" / "external"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Unprivileged symlink creation is unavailable")
    snapshot = scan_folder(root)
    assert snapshot.nodes[0].total == 18
    assert any(n.kind == "Link" for n in snapshot.nodes)
    plan = plan_flatten(snapshot)
    assert any(i.status == "Skipped" and "linked" in i.reason for i in plan.items)
    assert (target / "secret").read_bytes() == b"outside"
    with pytest.raises(ValueError, match="Links"):
        scan_folder(link)


def test_selection_and_filters_do_not_silently_limit_flatten(tmp_path):
    root = tree_fixture(tmp_path)
    snapshot = scan_folder(root)
    folder_view(snapshot, minimum=100000, depth=1, directories_only=True)
    plan = plan_flatten(snapshot, selected=[str(root / "ParentB")])
    assert [Path(i.source).name for i in plan.items if i.status == "Ready"] == ["Other"]
    for selected in ([], [str(root / "ParentA" / "Child2")], [str(root)]):
        with pytest.raises(ValueError, match="immediate"):
            plan_flatten(snapshot, selected=selected)


@pytest.mark.parametrize("include_files", [False, True])
@pytest.mark.parametrize("remove_empty", [False, True])
def test_flatten_preview_execution_undo_preserve_data(store, tmp_path, include_files, remove_empty):
    root = tree_fixture(tmp_path)
    before = files(root)
    snapshot = scan_folder(root)
    plan = plan_flatten(snapshot, include_files=include_files, remove_empty=remove_empty)
    projected = projected_snapshot(snapshot, plan)
    assert files(root) == before  # Both previews are read-only.
    assert projected.nodes[0].total == snapshot.nodes[0].total
    assert str(root / "Child2") in {n.path for n in projected.nodes}
    job = store.enqueue("Flatten", "flatten", plan.specs)
    assert execute_job(store, job) in ("succeeded", "completed with skips")
    assert (root / "Child2" / "a 日本.txt").read_bytes() == b"abc"
    assert (root / "root.txt").read_bytes() == b"z"
    assert (root / "ParentB").exists() == (not remove_empty)
    assert (root / "ParentA").exists() == (not remove_empty or not include_files)
    assert (root / "empty").exists()  # Unaffected empty folders are not deleted.
    undo_plan = plan_folder_undo(store, job)
    assert undo_plan.can_apply
    assert (root / "ParentB").exists() == (not remove_empty)  # Undo preview does not mkdir.
    undo = store.enqueue_undo(job, undo_plan.specs)
    assert execute_job(store, undo) in ("succeeded", "completed with skips")
    assert files(root) == before
    with pytest.raises(ValueError, match="already exists"):
        plan_folder_undo(store, job)
    with pytest.raises(ValueError, match="Redo"):
        plan_folder_undo(store, undo)


@pytest.mark.parametrize("collision", ["skip", "unique"])
def test_collision_across_parents_files_folders_and_existing_outputs(store, tmp_path, collision):
    root = tmp_path / "root"
    for parent in ("A", "B"):
        (root / parent / "same.ext").mkdir(parents=True)
        (root / parent / "same.ext" / "data").write_bytes(parent.encode())
        (root / parent / "same.txt").write_bytes(parent.encode())
    (root / "same.txt").write_bytes(b"existing")
    job, plan = flatten(store, root, include_files=True, collision=collision)
    assert execute_job(store, job) in ("succeeded", "completed with skips")
    assert (root / "same.txt").read_bytes() == b"existing"
    if collision == "unique":
        assert (root / "same.ext (2)" / "data").read_bytes() == b"B"
        assert (root / "same (2).txt").read_bytes() == b"A"
        assert (root / "same (3).txt").read_bytes() == b"B"
    else:
        assert sum(i.status == "Skipped" for i in plan.items) == 3
        assert (root / "B" / "same.ext" / "data").read_bytes() == b"B"
    undo = store.enqueue_undo(job, plan_folder_undo(store, job).specs)
    assert execute_job(store, undo) in ("succeeded", "completed with skips")
    assert (root / "A" / "same.ext" / "data").read_bytes() == b"A"
    assert (root / "B" / "same.ext" / "data").read_bytes() == b"B"


@pytest.mark.parametrize("change", ["edit", "add", "remove", "rename", "parent", "root", "destination"])
def test_changes_after_preview_block_batch_before_any_move(store, tmp_path, change):
    root = tree_fixture(tmp_path)
    job, plan = flatten(store, root)
    source = root / "ParentB" / "Other" / "c.txt"
    if change == "edit":
        source.write_bytes(b"edited")
    elif change == "add":
        source.with_name("new").write_bytes(b"new")
    elif change == "remove":
        source.unlink()
    elif change == "rename":
        source.rename(source.with_name("renamed"))
    elif change == "parent":
        (root / "ParentB").rename(root / "old-parent")
        (root / "ParentB").mkdir()
    elif change == "root":
        root.rename(tmp_path / "moved-root")
        root.mkdir()
    else:
        (root / "Other").mkdir()
    before = files(root)
    assert execute_job(store, job) == "failed"
    assert files(root) == before
    assert not (root / "Child2").exists()


@pytest.mark.parametrize("point", ["before_move", "before_remove"])
def test_arriving_parent_content_is_never_deleted(store, tmp_path, point):
    root = tree_fixture(tmp_path)
    job, plan = flatten(store, root, include_files=True)
    def hook(stage):
        if stage == ("move_intent" if point == "before_move" else "rmdir_intent"):
            parent = root / "ParentB"
            if parent.exists():
                (parent / "arrived.txt").write_bytes(b"keep me")
    assert execute_job(store, job, hook=hook) in ("succeeded", "completed with skips")
    assert (root / "ParentB" / "arrived.txt").read_bytes() == b"keep me"
    assert any(r["state"] == "kept" for r in cleanup_rows(store, job))


def test_destination_race_at_move_intent_is_not_overwritten(store, tmp_path):
    root = tree_fixture(tmp_path)
    job, plan = flatten(store, root)
    output = Path(plan.specs[0]["output"])
    def hook(stage):
        if stage == "move_intent":
            output.write_bytes(b"unrelated")
    assert execute_job(store, job, hook=hook) in ("failed", "interrupted")
    assert output.read_bytes() == b"unrelated"
    assert Path(plan.specs[0]["source"]).is_dir()


def test_cross_volume_failure_never_falls_back_to_copy_delete(store, tmp_path, monkeypatch):
    root = tree_fixture(tmp_path)
    before = files(root)
    job, plan = flatten(store, root)
    def refuse(*args):
        raise OSError(errno.EXDEV, "cross-volume move")
    monkeypatch.setattr("media_tool.folder_moves.move_no_replace", refuse)
    assert execute_job(store, job) in ("failed", "interrupted")
    assert files(root) == before


def test_permission_failure_keeps_files_and_retry_can_finish(store, tmp_path, monkeypatch):
    import media_tool.folder_moves as moves
    root = tree_fixture(tmp_path)
    before = files(root)
    job, plan = flatten(store, root)
    original = moves.move_no_replace
    monkeypatch.setattr(moves, "move_no_replace", lambda *args: (_ for _ in ()).throw(PermissionError("locked")))
    assert execute_job(store, job) == "failed"
    assert files(root) == before
    monkeypatch.setattr(moves, "move_no_replace", original)
    store.retry(job)
    assert execute_job(store, job) in ("succeeded", "completed with skips")


def test_source_mutation_at_move_intent_is_rechecked(store, tmp_path):
    root = tree_fixture(tmp_path)
    job, plan = flatten(store, root)
    def hook(point):
        if point == "move_intent":
            (Path(plan.specs[0]["source"]) / "arrived").write_bytes(b"keep")
    assert execute_job(store, job, hook=hook) == "failed"
    assert not Path(plan.specs[0]["output"]).exists()
    assert (Path(plan.specs[0]["source"]) / "arrived").read_bytes() == b"keep"


def test_changed_committed_output_is_not_adopted_on_retry(store, tmp_path):
    root = tree_fixture(tmp_path)
    job, plan = flatten(store, root)
    def hook(point):
        if point == "moved":
            (Path(plan.specs[0]["output"]) / "new.txt").write_bytes(b"external change")
    assert execute_job(store, job, hook=hook) == "failed"
    store.retry(job)
    assert execute_job(store, job) == "failed"
    assert (Path(plan.specs[0]["output"]) / "new.txt").read_bytes() == b"external change"
    assert (root / "ParentA" / "Child10").exists()


class Crash(BaseException):
    pass


@pytest.mark.parametrize("point", ["move_intent", "moved", "move_recorded", "rmdir_intent", "rmdir_removed"])
def test_crash_recovery_reconciles_atomic_moves_and_cleanup(tmp_path, point):
    root = tree_fixture(tmp_path)
    original_files = files(root)
    directory = tmp_path / "history"
    store = Store(directory)
    job, plan = flatten(store, root, include_files=True)
    def hook(stage):
        if stage == point:
            raise Crash()
    with pytest.raises(Crash):
        execute_job(store, job, hook=hook)
    store.close()
    store = Store(directory)
    try:
        store.recover()
        store.retry(job)
        assert execute_job(store, job) in ("succeeded", "completed with skips")
        undo = store.enqueue_undo(job, plan_folder_undo(store, job).specs)
        assert execute_job(store, undo) in ("succeeded", "completed with skips")
        assert files(root) == original_files
    finally:
        store.close()


@pytest.mark.parametrize("cancel_after_move", [False, True])
def test_cancel_and_retry_preserve_completed_moves(store, tmp_path, cancel_after_move):
    root = tree_fixture(tmp_path)
    before = files(root)
    job, plan = flatten(store, root)
    cancel = threading.Event()
    if not cancel_after_move:
        cancel.set()
    def hook(point):
        if point == "moved":
            cancel.set()
    assert execute_job(store, job, cancelled=cancel.is_set, hook=hook) == "cancelled"
    if not cancel_after_move:
        assert files(root) == before
    store.retry(job)
    assert execute_job(store, job) in ("succeeded", "completed with skips")
    undo = store.enqueue_undo(job, plan_folder_undo(store, job).specs)
    assert execute_job(store, undo) in ("succeeded", "completed with skips")
    assert files(root) == before


def test_pause_waits_between_moves_and_retry_checks_completed_outputs(store, tmp_path):
    root = tree_fixture(tmp_path)
    job, plan = flatten(store, root)
    pause = threading.Event()
    def hook(point):
        if point == "move_recorded":
            pause.set()
    assert execute_job(store, job, paused=pause.is_set, hook=hook) == "queued"
    assert sum(i["state"] == "succeeded" for i in store.items(job)) == 1
    pause.clear()
    assert execute_job(store, job) in ("succeeded", "completed with skips")


@pytest.mark.parametrize("change", ["edited", "added", "missing", "occupied", "parent_replaced"])
def test_undo_blocks_external_changes_and_unrelated_parent(store, tmp_path, change):
    root = tree_fixture(tmp_path)
    job, plan = flatten(store, root, include_files=True)
    assert execute_job(store, job) in ("succeeded", "completed with skips")
    if change == "edited":
        (root / "Other" / "c.txt").write_bytes(b"edited")
    elif change == "added":
        (root / "Other" / "added").write_bytes(b"added")
    elif change == "missing":
        (root / "Other" / "c.txt").unlink()
    elif change == "occupied":
        (root / "ParentB" / "Other").mkdir(parents=True)
    else:
        (root / "ParentB").mkdir()
    before = files(root)
    preview = plan_folder_undo(store, job)
    assert not preview.can_apply
    assert files(root) == before


@pytest.mark.parametrize("point,recoverable", [("mkdir_intent", True), ("mkdir_created", False), ("mkdir_recorded", True), ("moved", True)])
def test_undo_crash_recovery_never_adopts_unjournaled_parent(tmp_path, point, recoverable):
    root = tree_fixture(tmp_path)
    before = files(root)
    directory = tmp_path / "history"
    store = Store(directory)
    job, plan = flatten(store, root, include_files=True)
    assert execute_job(store, job) in ("succeeded", "completed with skips")
    undo = store.enqueue_undo(job, plan_folder_undo(store, job).specs)
    def hook(stage):
        if stage == point:
            raise Crash()
    with pytest.raises(Crash):
        execute_job(store, undo, hook=hook)
    store.close()
    store = Store(directory)
    try:
        store.recover()
        store.retry(undo)
        state = execute_job(store, undo)
        if recoverable:
            assert state == "succeeded"
            assert files(root) == before
        else:
            assert state == "failed"
            assert (root / "Other" / "c.txt").read_bytes() == b"12345"
            assert any(r["state"] == "recreating" for r in cleanup_rows(store, job))
    finally:
        store.close()


@pytest.mark.parametrize("tamper", ["outside", "relative", "traversal", "duplicate", "manifest"])
def test_saved_plan_constraints_reject_unsafe_moves(store, tmp_path, tamper):
    root = tree_fixture(tmp_path)
    plan = plan_flatten(scan_folder(root))
    specs = json.loads(json.dumps(plan.specs))
    if tamper == "outside":
        specs[0]["output"] = str(tmp_path / "outside")
    elif tamper == "relative":
        specs[0]["source"] = "relative"
    elif tamper == "traversal":
        specs[0]["output"] = str(root / ".." / "outside")
    elif tamper == "duplicate":
        specs.append(specs[0])
    else:
        specs[0]["members"][0]["relative"] = "../outside"
    with pytest.raises(ValueError):
        store.enqueue("Unsafe", "flatten", specs)
    assert not store.jobs()


def test_history_migrates_schema16_without_losing_jobs(tmp_path):
    directory = tmp_path / "history"
    store = Store(directory)
    job = store.enqueue("Earlier history", "sample", [{"name": "sample"}])
    store.db.execute("DROP TABLE folder_cleanup")
    store.db.execute("PRAGMA user_version=16")
    store.db.commit()
    store.close()
    store = Store(directory)
    try:
        assert store.jobs()[0]["id"] == job
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA
        assert cleanup_rows(store, job) == []
    finally:
        store.close()
