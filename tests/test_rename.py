from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

import pytest
from PySide6.QtTest import QTest
from media_tool.domain import FileRecord
from media_tool.jobs import SCHEMA, Store, execute_job
from media_tool.planning import PlanRequest, OperationPlan, PlanItem, build_plan
from media_tool.renaming import rename_snapshot
from test_foundation import app, window, wait_until


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "history")
    yield result
    result.close()


def fixture_plan(folder, names=("a.txt", "b.txt", "c.txt"), targets=None):
    folder.mkdir(exist_ok=True)
    records = []
    for index, name in enumerate(names):
        path = folder / name
        path.write_bytes(f"original contents {index}".encode())
        records.append(FileRecord.from_path(path))
    if targets is None:
        return build_plan(records, PlanRequest("rename", prefix="Photo_", start=7, padding=3))
    parent = folder.stat()
    return OperationPlan(PlanRequest("rename"), tuple(
        PlanItem(record, str(folder / target), "Planned", "Fixture", (parent.st_dev, parent.st_ino))
        for record, target in zip(records, targets)))


def assert_contents(plan):
    for index, item in enumerate(plan.items):
        output = Path(item.output if item.status == "Planned" else item.source.path)
        assert output.read_bytes() == f"original contents {index}".encode()
        assert output.stat().st_ino == item.source.inode
        assert output.stat().st_mtime_ns == item.source.modified_ns


def test_sequential_rename_contents_metadata_and_journal(store, tmp_path):
    plan = fixture_plan(tmp_path / "files", ("a 日本.TXT", "b.PNG", "c"))
    job = store.enqueue("Rename", "rename", rename_snapshot(plan))
    assert execute_job(store, job) == "succeeded"
    assert_contents(plan)
    assert all(item["rename_phase"] == "committed" for item in store.items(job))
    assert not list((tmp_path / "files").glob("*.stage"))


@pytest.mark.parametrize("targets", [("b.txt", "c.txt", "a.txt"), ("b.txt", "a.txt", "d.txt")])
def test_swaps_and_cycles(store, tmp_path, targets):
    plan = fixture_plan(tmp_path / "files", targets=targets)
    job = store.enqueue("Cycle", "rename", rename_snapshot(plan))
    assert execute_job(store, job) == "succeeded"
    assert_contents(plan)
    assert len(list((tmp_path / "files").iterdir())) == 3


def test_case_only_rename_uses_staging(store, tmp_path):
    plan = fixture_plan(tmp_path / "files", ("test.txt",), ("TEST.txt",))
    job = store.enqueue("Case", "rename", rename_snapshot(plan))
    assert execute_job(store, job) == "succeeded"
    assert [path.name for path in (tmp_path / "files").iterdir()] == ["TEST.txt"]
    assert_contents(plan)


def test_clean_separators_pattern_extension_and_collision(tmp_path):
    records = []
    for name in ("a__b--c.JPG", "a b c.JPG", "keep.txt"):
        path = tmp_path / name
        path.write_text(name)
        records.append(FileRecord.from_path(path))
    plan = build_plan(records, PlanRequest("rename", rename_style="clean", pattern="*.jpg"))
    by_name = {item.source.name: item for item in plan.items}
    assert by_name["a b c.JPG"].status == "Unchanged"
    assert Path(by_name["a__b--c.JPG"].output).name == "a b c_1.JPG"
    assert by_name["keep.txt"].status == "Skipped"


def test_random_names_frozen_and_seeded(tmp_path):
    plan = fixture_plan(tmp_path / "files")
    records = [item.source for item in plan.items]
    request = PlanRequest("rename", rename_style="random", seed="fixed")
    first, second = build_plan(records, request), build_plan(records, request)
    assert [item.output for item in first.items] == [item.output for item in second.items]
    assert all(Path(item.output).suffix == ".txt" for item in first.items)
    assert len(set(item.output for item in first.items)) == 3


def test_skipped_dependency_propagates_to_sources_that_stay(tmp_path):
    for name in ("n_0000.txt", "n_0001.txt", "n_0002.txt", "n_0003.txt"):
        (tmp_path / name).write_text(name)
    records = [FileRecord.from_path(tmp_path / name) for name in ("n_0000.txt", "n_0001.txt", "n_0002.txt")]
    plan = build_plan(records, PlanRequest("rename", prefix="n_", collision="skip"))
    assert all(item.status == "Skipped" for item in plan.items)


@pytest.mark.parametrize("point", ["stage_intent", "staged", "all_staged", "commit_intent", "committed"])
def test_actual_process_crash_and_retry_cycle(store, tmp_path, point):
    plan = fixture_plan(tmp_path / "files", targets=("b.txt", "c.txt", "a.txt"))
    job = store.enqueue("Crash", "rename", rename_snapshot(plan))
    code = """import os,sys
from media_tool.jobs import Store,execute_job
s=Store(sys.argv[1])
def stop(point):
    if point == sys.argv[3]: os._exit(73)
execute_job(s,sys.argv[2],hook=stop)
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    result = subprocess.run([sys.executable, "-c", code, str(store.directory), job, point],
                            env=environment, capture_output=True, timeout=15)
    assert result.returncode == 73, result.stderr.decode(errors="replace")
    store.recover()
    assert store.retry(job) == 3
    assert execute_job(store, job) == "succeeded"
    assert_contents(plan)
    assert len(list((tmp_path / "files").iterdir())) == 3


@pytest.mark.parametrize("pause", [False, True])
def test_staging_cancel_or_pause_restores_names(store, tmp_path, pause):
    plan = fixture_plan(tmp_path / "files", targets=("b.txt", "c.txt", "a.txt"))
    job = store.enqueue("Stop", "rename", rename_snapshot(plan))
    stop = threading.Event()
    def hook(point):
        if point == "staged":
            stop.set()
    kwargs = {"paused" if pause else "cancelled": stop.is_set}
    assert execute_job(store, job, hook=hook, **kwargs) == ("queued" if pause else "cancelled")
    for item in plan.items:
        assert Path(item.source.path).stat().st_ino == item.source.inode
    assert not list((tmp_path / "files").glob("*.stage"))
    if not pause:
        store.retry(job)
    assert execute_job(store, job) == "succeeded"
    assert_contents(plan)


def test_cancel_after_commit_begins_finishes_the_cycle(store, tmp_path):
    plan = fixture_plan(tmp_path / "files", targets=("b.txt", "c.txt", "a.txt"))
    job = store.enqueue("Finish cycle", "rename", rename_snapshot(plan))
    stop = threading.Event()
    assert execute_job(store, job, cancelled=stop.is_set,
                       hook=lambda point: stop.set() if point == "committed" else None) == "succeeded"
    assert_contents(plan)


def test_source_changed_before_run_blocks_whole_batch(store, tmp_path):
    plan = fixture_plan(tmp_path / "files")
    job = store.enqueue("Invalid", "rename", rename_snapshot(plan))
    Path(plan.items[-1].source.path).write_text("edited")
    assert execute_job(store, job) == "failed"
    assert all(Path(item.source.path).exists() for item in plan.items)
    assert not any(Path(item.output).exists() for item in plan.items)


def test_late_collision_keeps_good_file_and_restores_originals(store, tmp_path):
    plan = fixture_plan(tmp_path / "files")
    job = store.enqueue("Collision", "rename", rename_snapshot(plan))
    def collide(point):
        if point == "all_staged":
            Path(plan.items[0].output).write_text("arrived")
    assert execute_job(store, job, hook=collide) == "failed"
    assert Path(plan.items[0].output).read_text() == "arrived"
    assert all(Path(item.source.path).exists() for item in plan.items)


def test_partial_commit_collision_retains_stages_and_retry_skips_success(store, tmp_path):
    plan = fixture_plan(tmp_path / "files", targets=("b.txt", "c.txt", "a.txt"))
    job = store.enqueue("Partial cycle", "rename", rename_snapshot(plan))
    called = []
    def collide(point):
        if point == "committed" and not called:
            called.append(True)
            Path(plan.items[1].output).write_text("good existing file")
    assert execute_job(store, job, hook=collide) == "failed"
    rows = store.items(job)
    assert rows[0]["state"] == "succeeded"
    assert all(Path(item["stage"]).exists() for item in rows[1:])
    assert Path(plan.items[1].output).read_text() == "good existing file"
    Path(plan.items[1].output).unlink()
    assert store.retry(job) == 2
    assert execute_job(store, job) == "succeeded"
    assert store.items(job)[0]["attempts"] == 1
    assert_contents(plan)


def test_schema_one_history_upgrades_without_losing_copy_jobs(tmp_path):
    directory = tmp_path / "old-history"
    directory.mkdir()
    with sqlite3.connect(directory / "history.sqlite3") as connection:
        connection.executescript("""
            CREATE TABLE jobs(id TEXT PRIMARY KEY,title TEXT,adapter TEXT,created TEXT,state TEXT);
            CREATE TABLE items(id TEXT PRIMARY KEY,job_id TEXT,ordinal INTEGER,spec TEXT,state TEXT,
                attempts INTEGER DEFAULT 0,message TEXT DEFAULT '',temp TEXT,temp_dev TEXT,temp_ino TEXT,
                digest TEXT,updated TEXT);
            PRAGMA user_version=1;
            INSERT INTO jobs VALUES ('saved','Old sample','sample','2026','queued');
            INSERT INTO items(id,job_id,ordinal,spec,state,updated)
                VALUES ('item','saved',0,'{"name":"old","seconds":0}','queued','2026');
        """)
    store = Store(directory)
    try:
        assert execute_job(store, "saved") == "succeeded"
        assert store.item("item")["rename_phase"] == "pending"
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA
    finally:
        store.close()


def test_ui_preview_queue_run_and_refresh_names(window, tmp_path):
    folder = tmp_path / "inputs"
    plan = fixture_plan(folder)
    window.start_scan([str(folder)])
    wait_until(lambda: not window.busy)
    window._tool_changed("Rename")
    window.prefix.setText("Picture_")
    window.start_number.setValue(5)
    window.preview_changes()
    wait_until(lambda: not window.busy)
    assert window.run_button.isEnabled()
    expected = [Path(item.output).name for item in window.last_plan.items]
    window.run_button.click()
    queue = window.jobs_dialog
    queue.start_queue()
    wait_until(lambda: queue.worker is None and not queue.enabled)
    assert window.model.rowCount() == 0
    assert [Path(item["output"]).name for item in window.result_model.items] == expected
    assert window.plan_model.rowCount() == 0
    assert set(record.identity_key for record in window.model.items) == window.model.paths
    assert queue.store.jobs()[0]["state"] == "succeeded"
    assert all(path.name.startswith("Picture_") for path in folder.iterdir())


def test_ui_invalid_preview_cannot_queue(window, tmp_path):
    plan = fixture_plan(tmp_path / "inputs")
    window.start_scan([item.source.path for item in plan.items])
    wait_until(lambda: not window.busy)
    window._tool_changed("Rename")
    window.prefix.setText("bad/name")
    window.preview_changes()
    wait_until(lambda: not window.busy)
    assert not window.run_button.isEnabled()


def test_ui_naming_style_options(window):
    window._tool_changed("Rename")
    assert window.format.count() == 3
    window.format.setCurrentIndex(2)
    assert window.separator.isVisible()
    assert not window.start_number.isVisible()
    assert window.rename_pattern.isVisible()
    window._tool_changed("Images")
    assert not window.separator.isVisible()
    assert not window.rename_pattern.isVisible()


def test_cancel_with_original_name_taken_retains_staged_file(store, tmp_path):
    plan = fixture_plan(tmp_path / "files", targets=("b.txt", "c.txt", "a.txt"))
    job = store.enqueue("Restore conflict", "rename", rename_snapshot(plan))
    stop = threading.Event()
    def collide(point):
        if point == "staged" and not stop.is_set():
            Path(plan.items[0].source.path).write_text("new unrelated file")
            stop.set()
    assert execute_job(store, job, cancelled=stop.is_set, hook=collide) == "failed"
    first = store.items(job)[0]
    assert Path(first["stage"]).read_bytes() == b"original contents 0"
    assert Path(plan.items[0].source.path).read_text() == "new unrelated file"
    Path(plan.items[0].source.path).unlink()
    store.retry(job)
    assert execute_job(store, job) == "succeeded"
    assert_contents(plan)


@pytest.mark.parametrize("when", ["stage", "commit"])
def test_move_succeeds_then_reports_failure_is_reconciled(store, tmp_path, monkeypatch, when):
    import media_tool.renaming as renaming
    plan = fixture_plan(tmp_path / "files", targets=("b.txt", "c.txt", "a.txt"))
    job = store.enqueue("After-move error", "rename", rename_snapshot(plan))
    original = renaming.move_no_replace
    injected = []
    def move(source, output):
        original(source, output)
        is_stage = str(output).endswith(".stage")
        if not injected and is_stage == (when == "stage"):
            injected.append(True)
            raise OSError("Injected failure after successful move")
    monkeypatch.setattr(renaming, "move_no_replace", move)
    assert execute_job(store, job) == "failed"
    store.retry(job)
    assert execute_job(store, job) == "succeeded"
    assert_contents(plan)


def test_foreign_staging_file_is_never_removed(store, tmp_path):
    plan = fixture_plan(tmp_path / "files")
    job = store.enqueue("Stage occupied", "rename", rename_snapshot(plan))
    path = Path(store.items(job)[0]["stage"])
    path.write_text("not this job's file")
    assert execute_job(store, job) == "failed"
    assert path.read_text() == "not this job's file"
    assert all(Path(item.source.path).exists() for item in plan.items)


def test_source_edit_after_intent_is_not_staged(store, tmp_path):
    plan = fixture_plan(tmp_path / "files")
    job = store.enqueue("Source edited", "rename", rename_snapshot(plan))
    def change(point):
        if point == "stage_intent":
            Path(plan.items[0].source.path).write_text("edited")
    assert execute_job(store, job, hook=change) == "failed"
    assert Path(plan.items[0].source.path).read_text() == "edited"
    assert not list((tmp_path / "files").glob("*.stage"))


@pytest.mark.skipif(os.name != "nt", reason="Windows open-file sharing behavior")
def test_open_file_denied_rename_preserves_original_names(store, tmp_path):
    plan = fixture_plan(tmp_path / "files")
    job = store.enqueue("Open file", "rename", rename_snapshot(plan))
    with Path(plan.items[0].source.path).open("rb"):
        assert execute_job(store, job) == "failed"
    assert all(Path(item.source.path).exists() for item in plan.items)
    assert not list((tmp_path / "files").glob("*.stage"))


@pytest.mark.parametrize("point", ["rollback_intent", "rolled_back"])
def test_crash_during_rollback_then_retry(store, tmp_path, point):
    plan = fixture_plan(tmp_path / "files", targets=("b.txt", "c.txt", "a.txt"))
    job = store.enqueue("Rollback crash", "rename", rename_snapshot(plan))
    code = """import os,sys,threading
from media_tool.jobs import Store,execute_job
s=Store(sys.argv[1])
cancel=threading.Event()
def stop(point):
    if point == 'staged': cancel.set()
    if point == sys.argv[3]: os._exit(73)
execute_job(s,sys.argv[2],cancelled=cancel.is_set,hook=stop)
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    result = subprocess.run([sys.executable, "-c", code, str(store.directory), job, point],
                            env=environment, capture_output=True, timeout=15)
    assert result.returncode == 73, result.stderr.decode(errors="replace")
    store.recover()
    store.retry(job)
    assert execute_job(store, job) == "succeeded"
    assert_contents(plan)


@pytest.mark.parametrize("targets", [("same.txt", "same.txt", "other.txt"),
                                   ("../escaped.txt", "other.txt", "last.txt")])
def test_invalid_complete_manifest_is_rejected_before_enqueue(tmp_path, targets):
    plan = fixture_plan(tmp_path / "files", targets=targets)
    with pytest.raises(ValueError):
        rename_snapshot(plan)
    assert all(Path(item.source.path).exists() for item in plan.items)


def test_clean_style_ignores_hidden_prefix_setting(tmp_path):
    path = tmp_path / "a__b.txt"
    path.write_text("keep")
    plan = build_plan([FileRecord.from_path(path)], PlanRequest("rename", prefix="invalid/name", rename_style="clean"))
    assert Path(plan.items[0].output).name == "a b.txt"
