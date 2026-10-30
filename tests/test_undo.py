import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

import pytest

from media_tool.jobs import SCHEMA, Store, execute_job
from media_tool.jobs_ui import QueueDialog
from media_tool.renaming import rename_snapshot
from media_tool.undo import inverse_specs, plan_undo
from test_foundation import app, window, wait_until
from test_rename import fixture_plan, store


def renamed(store, tmp_path, names=("a.txt", "b.txt", "c.txt"), targets=None):
    plan = fixture_plan(tmp_path / "files", names, targets)
    job = store.enqueue("Rename", "rename", rename_snapshot(plan))
    assert execute_job(store, job) in ("succeeded", "completed with skips")
    return job, plan


def assert_restored(plan):
    for index, item in enumerate(plan.items):
        path = Path(item.source.path)
        assert path.read_bytes() == f"original contents {index}".encode()
        assert path.stat().st_ino == item.source.inode
        assert path.stat().st_mtime_ns == item.source.modified_ns
    assert not list(Path(plan.items[0].source.path).parent.glob("*.stage"))


@pytest.mark.parametrize("names,targets", [
    (("a 日本.TXT", "b.PNG", "c"), None),
    (("a.txt", "b.txt", "c.txt"), ("b.txt", "c.txt", "a.txt")),
    (("a.txt", "b.txt", "c.txt"), ("b.txt", "a.txt", "d.txt")),
    (("test.txt",), ("TEST.txt",)),
])
def test_undo_restores_exact_names_identity_and_contents(store, tmp_path, names, targets):
    job, plan = renamed(store, tmp_path, names, targets)
    before = {path.name: path.read_bytes() for path in (tmp_path / "files").iterdir()}
    preview = plan_undo(store, job)
    assert preview.can_apply
    assert before == {path.name: path.read_bytes() for path in (tmp_path / "files").iterdir()}
    undo = store.enqueue_undo(job, preview.specs)
    assert store.undo_job(job) == undo
    assert store.original_job(undo) == job
    assert execute_job(store, undo) == "succeeded"
    assert_restored(plan)
    assert all(item["rename_phase"] == "committed" for item in store.items(undo))
    with pytest.raises(ValueError, match="already exists"):
        plan_undo(store, job)
    with pytest.raises(ValueError, match="Redo"):
        plan_undo(store, undo)


@pytest.mark.parametrize("change", ["edited", "missing", "moved", "replaced", "original_taken", "parent_replaced"])
def test_undo_preview_blocks_changed_files_and_conflicts(store, tmp_path, change):
    job, plan = renamed(store, tmp_path)
    output, original = Path(plan.items[0].output), Path(plan.items[0].source.path)
    if change == "edited":
        output.write_bytes(b"edited content")
    elif change == "missing":
        output.unlink()
    elif change == "moved":
        output.rename(output.with_name("moved.txt"))
    elif change == "replaced":
        replacement = output.with_name("replacement.txt")
        replacement.write_bytes(output.read_bytes())
        replacement.replace(output)
    elif change == "original_taken":
        original.write_bytes(b"unrelated file")
    else:
        folder = output.parent
        folder.rename(tmp_path / "moved-parent")
        folder.mkdir()
    before = {path: path.read_bytes() for path in (tmp_path / "files").iterdir() if path.is_file()}
    preview = plan_undo(store, job)
    assert not preview.can_apply
    assert any(item.status == "Conflict" for item in preview.items)
    assert before == {path: path.read_bytes() for path in (tmp_path / "files").iterdir() if path.is_file()}
    assert store.undo_job(job) is None


def test_changes_after_preview_are_rechecked_before_any_move(store, tmp_path):
    job, plan = renamed(store, tmp_path)
    preview = plan_undo(store, job)
    Path(plan.items[-1].source.path).write_bytes(b"arrived after preview")
    undo = store.enqueue_undo(job, preview.specs)
    assert execute_job(store, undo) == "failed"
    assert all(Path(item.output).exists() for item in plan.items)
    assert Path(plan.items[-1].source.path).read_bytes() == b"arrived after preview"
    assert not list((tmp_path / "files").glob("*.stage"))


def test_only_successful_renames_enter_undo(store, tmp_path):
    plan = fixture_plan(tmp_path / "files")
    specs = list(rename_snapshot(plan))
    specs[-1]["initial_state"] = "skipped"
    job = store.enqueue("With skip", "rename", specs)
    assert execute_job(store, job) == "completed with skips"
    preview = plan_undo(store, job)
    assert len(preview.items) == 2
    undo = store.enqueue_undo(job, preview.specs)
    assert execute_job(store, undo) == "succeeded"
    assert_restored(plan)


def test_incomplete_original_and_nonrename_cannot_be_undone(store, tmp_path):
    plan = fixture_plan(tmp_path / "files")
    job = store.enqueue("Waiting", "rename", rename_snapshot(plan))
    with pytest.raises(ValueError, match="Finish or retry"):
        plan_undo(store, job)
    sample = store.enqueue("Sample", "sample", [{"name": "test", "seconds": 0}])
    execute_job(store, sample)
    with pytest.raises(ValueError, match="completed rename"):
        plan_undo(store, sample)


def test_undo_link_is_atomic_and_prevents_duplicate_jobs(store, tmp_path):
    job, plan = renamed(store, tmp_path)
    preview = plan_undo(store, job)
    store.db.execute("""CREATE TRIGGER reject_undo BEFORE INSERT ON rename_undos
        BEGIN SELECT RAISE(ABORT, 'injected link failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        store.enqueue_undo(job, preview.specs)
    assert len(store.jobs()) == 1
    assert store.undo_job(job) is None
    store.db.execute("DROP TRIGGER reject_undo")
    undo = store.enqueue_undo(job, preview.specs)
    with pytest.raises(ValueError, match="already exists"):
        store.enqueue_undo(job, preview.specs)
    assert len(store.jobs()) == 2
    assert store.undo_job(job) == undo


def test_undo_cannot_change_the_saved_original_names(store, tmp_path):
    job, plan = renamed(store, tmp_path)
    specs = [dict(spec) for spec in plan_undo(store, job).specs]
    specs[0]["output"] = str(tmp_path / "files" / "invented.txt")
    with pytest.raises(ValueError, match="no longer matches"):
        store.enqueue_undo(job, specs)
    assert len(store.jobs()) == 1


def test_partial_undo_preserves_foreign_file_and_retry_finishes(store, tmp_path):
    job, plan = renamed(store, tmp_path)
    undo = store.enqueue_undo(job, plan_undo(store, job).specs)
    arrivals = []
    def collide(point):
        if point == "committed" and not arrivals:
            arrivals.append(True)
            Path(plan.items[1].source.path).write_bytes(b"foreign file")
    assert execute_job(store, undo, hook=collide) == "failed"
    first = store.items(undo)[0]
    assert first["state"] == "succeeded"
    assert Path(plan.items[1].source.path).read_bytes() == b"foreign file"
    assert all(Path(item["stage"]).exists() for item in store.items(undo)[1:])
    with pytest.raises(ValueError, match="already exists"):
        plan_undo(store, job)
    Path(plan.items[1].source.path).unlink()
    assert store.retry(undo) == 2
    assert execute_job(store, undo) == "succeeded"
    assert store.items(undo)[0]["attempts"] == first["attempts"]
    assert_restored(plan)


@pytest.mark.parametrize("point", ["stage_intent", "staged", "all_staged", "commit_intent", "committed", "rollback_intent", "rolled_back"])
def test_actual_crash_during_undo_can_recover_and_retry(store, tmp_path, point):
    job, plan = renamed(store, tmp_path, targets=("b.txt", "c.txt", "a.txt"))
    undo = store.enqueue_undo(job, plan_undo(store, job).specs)
    code = '''import os,sys,threading
from media_tool.jobs import Store,execute_job
store=Store(sys.argv[1])
cancel=threading.Event()
def hook(point):
    if sys.argv[3] in ('rollback_intent','rolled_back') and point=='staged': cancel.set()
    if point==sys.argv[3]: os._exit(73)
execute_job(store,sys.argv[2],cancelled=cancel.is_set,hook=hook)
'''
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    result = subprocess.run([sys.executable, "-c", code, str(store.directory), undo, point],
                            env=env, capture_output=True, timeout=15)
    assert result.returncode == 73, result.stderr.decode(errors="replace")
    store.recover()
    store.retry(undo)
    assert store.undo_job(job) == undo
    assert execute_job(store, undo) == "succeeded"
    assert_restored(plan)


def test_cancel_undo_before_commit_keeps_renamed_names(store, tmp_path):
    job, plan = renamed(store, tmp_path)
    undo = store.enqueue_undo(job, plan_undo(store, job).specs)
    cancel = threading.Event()
    assert execute_job(store, undo, cancelled=cancel.is_set,
                       hook=lambda point: cancel.set() if point == "staged" else None) == "cancelled"
    assert all(Path(item.output).exists() for item in plan.items)
    assert not any(Path(item.source.path).exists() for item in plan.items)
    assert not list((tmp_path / "files").glob("*.stage"))
    store.retry(undo)
    assert execute_job(store, undo) == "succeeded"
    assert_restored(plan)


def test_schema_two_completed_rename_remains_undoable(tmp_path):
    store = Store(tmp_path / "history")
    job, plan = renamed(store, tmp_path)
    store.db.execute("DROP TABLE rename_undos")
    store.db.execute("PRAGMA user_version=2")
    store.close()
    store = Store(tmp_path / "history")
    try:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA
        undo = store.enqueue_undo(job, plan_undo(store, job).specs)
        assert execute_job(store, undo) == "succeeded"
        assert_restored(plan)
    finally:
        store.close()


def ui_rename(window, tmp_path):
    source = tmp_path / "original.txt"
    source.write_bytes(b"keep contents")
    window.start_scan([str(source)])
    wait_until(lambda: not window.busy)
    window._tool_changed("Rename")
    window.start_rename_button.click()
    wait_until(lambda: not window.busy and window.jobs_dialog.worker is None)
    return source, window.jobs_dialog, window.last_result_job


def test_ui_undo_from_results_without_opening_queue(window, tmp_path):
    source, queue, job = ui_rename(window, tmp_path)
    waiting = queue.store.enqueue("Waiting", "sample", [{"name": "later", "seconds": 0}])
    assert window.preview_undo_button.isVisible()
    window.preview_undo_button.click()
    wait_until(lambda: not window.busy)
    assert window.undo_plan.can_apply
    assert not source.exists()
    assert window.tabs.currentIndex() == 1
    assert window.apply_undo_button.isEnabled()
    assert not window.start_rename_button.isEnabled()
    window.apply_undo_button.click()
    window.apply_undo_button.click()
    wait_until(lambda: not window.busy and queue.worker is None)
    assert source.read_bytes() == b"keep contents"
    assert window.tabs.currentIndex() == 2
    assert window.result_model.items[0]["output"] == str(source)
    assert window.visual.activity.title.text()=='Succeeded'
    assert '1 completed' in window.visual.activity.counts.text()
    assert not window.preview_undo_button.isVisible()
    assert queue.store.items(waiting)[0]["attempts"] == 0
    assert len(queue.store.jobs()) == 3
    assert not queue.isVisible()


def test_ui_conflict_blocks_apply_until_fresh_preview(window, tmp_path):
    source, queue, job = ui_rename(window, tmp_path)
    source.write_bytes(b"new unrelated file")
    assert window.preview_undo(job)
    wait_until(lambda: not window.busy)
    assert not window.undo_plan.can_apply
    assert not window.apply_undo_button.isEnabled()
    window.apply_undo()
    assert len(queue.store.jobs()) == 1
    assert source.read_bytes() == b"new unrelated file"
    source.unlink()
    window.discard_undo_button.click()
    window.preview_undo_button.click()
    wait_until(lambda: not window.busy)
    assert window.apply_undo_button.isEnabled()


def test_ui_undo_after_restart_from_saved_history(window, tmp_path):
    source, queue, job = ui_rename(window, tmp_path)
    directory = queue.directory
    assert queue.shutdown()
    window.jobs_dialog = QueueDialog(window, directory)
    queue = window.jobs_dialog
    window.open_jobs()
    queue.refresh(job)
    assert queue.undo_button.isEnabled()
    queue.undo_button.click()
    wait_until(lambda: not window.busy)
    assert not queue.isVisible()
    assert window.undo_plan.original_job == job
    window.apply_undo_button.click()
    wait_until(lambda: not window.busy and queue.worker is None)
    assert source.exists()
    queue.refresh(job)
    assert queue.undo_button.text() == "View undo"
    queue.undo_button.click()
    assert queue.selected_job() == queue.store.undo_job(job)


def test_ui_late_edit_after_undo_preview_fails_without_overwrite(window, tmp_path):
    source, queue, job = ui_rename(window, tmp_path)
    window.preview_undo(job)
    wait_until(lambda: not window.busy)
    output = Path(window.undo_plan.specs[0]["source"])
    output.write_bytes(b"edited after preview")
    window.apply_undo_button.click()
    wait_until(lambda: not window.busy and queue.worker is None)
    assert not source.exists()
    assert output.read_bytes() == b"edited after preview"
    assert window.result_model.items[0]["state"] == "failed"
    assert window.visual.activity.title.text()=='Failed'
    assert window.visual.activity.detail.text()
