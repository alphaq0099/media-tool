import errno
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

import pytest
from PySide6.QtCore import QTimer
from media_tool.domain import FileRecord
from media_tool.jobs import Store, OwnerLock, copy_snapshot, execute_job
from media_tool.jobs_ui import QueueDialog
from test_foundation import app, window, wait_until


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "history")
    yield value
    value.close()


def copy_job(store, tmp_path, count=1, content=b"original data"):
    inputs = tmp_path / "inputs"
    output = tmp_path / "output"
    inputs.mkdir(exist_ok=True)
    output.mkdir(exist_ok=True)
    sources = []
    for index in range(count):
        source = inputs / f"file {index + 1} 日本.txt"
        source.write_bytes(content)
        sources.append(source)
    specs = copy_snapshot([FileRecord.from_path(source) for source in sources], output)
    return store.enqueue("Copies", "copy", specs), sources, output


def test_manifest_is_read_only_unique_and_frozen(store, tmp_path):
    source = tmp_path / "a.txt"
    source.write_text("keep")
    output = tmp_path / "output"
    output.mkdir()
    (output / source.name).write_text("existing")
    before = source.stat()
    specs = copy_snapshot([FileRecord.from_path(source)], output)
    assert Path(specs[0]["output"]).name == "a (2).txt"
    job = store.enqueue("Copy", "copy", specs)
    specs[0]["output"] = "mutated UI value"
    assert json.loads(store.items(job)[0]["spec"])["output"].endswith("a (2).txt")
    assert len(list(output.iterdir())) == 1
    assert source.stat() == before


def test_verified_copy_preserves_source_and_history(store, tmp_path):
    job, sources, output = copy_job(store, tmp_path, 2, b"fixture" * 300000)
    before = [(source.stat(), hashlib.sha256(source.read_bytes()).hexdigest()) for source in sources]
    assert execute_job(store, job) == "succeeded"
    for index, item in enumerate(store.items(job)):
        spec = json.loads(item["spec"])
        assert hashlib.sha256(Path(spec["output"]).read_bytes()).hexdigest() == before[index][1]
        actual, original = sources[index].stat(), before[index][0]
        assert (actual.st_ino, actual.st_size, actual.st_mtime_ns) == (original.st_ino, original.st_size, original.st_mtime_ns)
        assert hashlib.sha256(sources[index].read_bytes()).hexdigest() == before[index][1]
        assert item["digest"] == before[index][1]
    assert not list(output.glob("*.part"))
    assert any(event["state"] == "committing" for event in store.events(job))


@pytest.mark.parametrize("point", ["created", "written", "prepared", "committed"])
def test_real_process_crash_and_restart(store, tmp_path, point):
    job, sources, output = copy_job(store, tmp_path, content=b"restart" * 10000)
    directory = store.directory
    # sqlite permits this worker connection; no parent mutations while it runs.
    code = """import os,sys
from media_tool.jobs import Store,execute_job
s=Store(sys.argv[1])
def crash(point):
    if point == sys.argv[3]:
        os._exit(73)
execute_job(s,sys.argv[2],hook=crash)
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    result = subprocess.run([sys.executable, "-c", code, str(directory), job, point],
                            env=environment, timeout=15, capture_output=True)
    assert result.returncode == 73, result.stderr.decode(errors="replace")
    assert store.recover() == 1
    assert store.items(job)[0]["state"] == "interrupted"
    assert store.retry(job) == 1
    assert execute_job(store, job) == "succeeded"
    assert (output / sources[0].name).read_bytes() == sources[0].read_bytes()
    assert len(list(output.iterdir())) == 1
    if point == "committed":
        assert "Recovered" in store.items(job)[0]["message"]


def test_late_collision_never_overwrites(store, tmp_path):
    job, sources, output = copy_job(store, tmp_path)
    target = output / sources[0].name
    def collide(point):
        if point == "prepared":
            target.write_bytes(b"arrived during copy")
    assert execute_job(store, job, hook=collide) == "failed"
    assert target.read_bytes() == b"arrived during copy"
    assert len(list(output.iterdir())) == 1


@pytest.mark.parametrize("point", ["created", "chunk", "prepared"])
def test_disk_full_or_denied_leaves_no_partial_output(store, tmp_path, point):
    job, sources, output = copy_job(store, tmp_path, content=b"safe" * 500000)
    before = sources[0].read_bytes()
    def failure(stage):
        if stage == point:
            raise OSError(errno.ENOSPC if point == "chunk" else errno.EACCES, "Injected write failure")
    assert execute_job(store, job, hook=failure) == "failed"
    assert list(output.iterdir()) == []
    assert sources[0].read_bytes() == before


@pytest.mark.parametrize("point", ["created", "prepared", "committed"])
def test_cancel_checkpoint_and_retry(store, tmp_path, point):
    job, sources, output = copy_job(store, tmp_path, 2)
    cancel = threading.Event()
    def stop(stage):
        if stage == point:
            cancel.set()
    assert execute_job(store, job, cancel.is_set, hook=stop) == "cancelled"
    states = [item["state"] for item in store.items(job)]
    assert states == (["succeeded", "cancelled"] if point == "committed" else ["cancelled", "cancelled"])
    assert store.retry(job) == (1 if point == "committed" else 2)
    assert execute_job(store, job) == "succeeded"
    assert len(list(output.iterdir())) == 2


def test_cancel_before_start_creates_nothing(store, tmp_path):
    job, _, output = copy_job(store, tmp_path, 2)
    assert execute_job(store, job, cancelled=lambda: True) == "cancelled"
    assert not list(output.iterdir())
    assert all(item["attempts"] == 0 for item in store.items(job))


def test_pause_finishes_only_current_item(store, tmp_path):
    job, _, output = copy_job(store, tmp_path, 3)
    pause = threading.Event()
    assert execute_job(store, job, paused=pause.is_set, hook=lambda point: pause.set()) == "queued"
    assert [item["state"] for item in store.items(job)] == ["succeeded", "queued", "queued"]
    assert execute_job(store, job) == "succeeded"
    assert len(list(output.iterdir())) == 3


@pytest.mark.parametrize("during_copy", [False, True])
def test_changed_source_is_rejected(store, tmp_path, during_copy):
    job, sources, output = copy_job(store, tmp_path)
    def change(stage):
        if stage == "written":
            sources[0].write_bytes(b"edited while copying")
    if not during_copy:
        sources[0].write_bytes(b"edited after queueing")
    assert execute_job(store, job, hook=change if during_copy else lambda stage: None) == "failed"
    assert not list(output.iterdir())
    assert "Source changed" in store.items(job)[0]["message"]


def test_cleanup_never_deletes_replaced_temp(store, tmp_path):
    job, _, output = copy_job(store, tmp_path)
    class Crash(BaseException):
        pass
    def stop(stage):
        if stage == "written":
            raise Crash()
    with pytest.raises(Crash):
        execute_job(store, job, hook=stop)
    item = store.items(job)[0]
    temp = Path(item["temp"])
    temp.rename(output / "original temporary file")
    temp.write_bytes(b"unrelated file")
    store.recover()
    store.retry(job)
    assert execute_job(store, job) == "failed"
    assert temp.read_bytes() == b"unrelated file"
    assert (output / "original temporary file").exists()


def test_recovery_does_not_claim_matching_unowned_output(store, tmp_path):
    job, sources, output = copy_job(store, tmp_path)
    class Crash(BaseException):
        pass
    def stop(stage):
        if stage == "prepared":
            raise Crash()
    with pytest.raises(Crash):
        execute_job(store, job, hook=stop)
    target = output / sources[0].name
    target.write_bytes(sources[0].read_bytes())
    store.recover()
    store.retry(job)
    assert execute_job(store, job) == "failed"
    assert target.read_bytes() == sources[0].read_bytes()
    assert "Recovered" not in store.items(job)[0]["message"]


def test_history_failure_after_commit_recovers_without_second_copy(store, tmp_path, monkeypatch):
    job, sources, output = copy_job(store, tmp_path)
    original = store.set_item
    def fail_success(item_id, state, *args, **kwargs):
        if state == "succeeded":
            raise OSError(errno.ENOSPC, "History disk full")
        return original(item_id, state, *args, **kwargs)
    monkeypatch.setattr(store, "set_item", fail_success)
    assert execute_job(store, job) == "failed"
    monkeypatch.setattr(store, "set_item", original)
    store.retry(job)
    assert execute_job(store, job) == "succeeded"
    assert len(list(output.iterdir())) == 1
    assert "Recovered" in store.items(job)[0]["message"]


def test_retry_keeps_successful_attempts(store):
    job = store.enqueue("Sample", "sample", [
        {"name": "first", "seconds": 0},
        {"name": "second", "seconds": 0, "fail_once": True}])
    assert execute_job(store, job) == "failed"
    assert store.retry(job) == 1
    assert execute_job(store, job) == "succeeded"
    assert [item["attempts"] for item in store.items(job)] == [1, 2]


def test_owner_lock_prevents_second_instance(tmp_path):
    first = OwnerLock(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="another app instance"):
            OwnerLock(tmp_path)
    finally:
        first.close()
    second = OwnerLock(tmp_path)
    second.close()


def test_unknown_schema_left_intact(tmp_path):
    path = tmp_path / "history.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version=999")
    connection.close()
    with pytest.raises(ValueError, match="Unsupported"):
        Store(tmp_path)
    with sqlite3.connect(path) as check:
        assert check.execute("PRAGMA user_version").fetchone()[0] == 999


def test_sidebar_theme_and_lazy_queue(window, tmp_path):
    assert window.jobs_dialog is None
    assert not (tmp_path / "jobs").exists()
    layout = window.sidebar_layout
    assert layout.itemAt(layout.count() - 1).widget() is window.theme_button
    assert window.theme_button.parentWidget().objectName() == "sidebar"
    window.set_theme('light')
    window.theme_button.click()
    assert window.theme == "dark"


def test_queue_ui_retry_and_intake_remains_responsive(window, tmp_path):
    queue = QueueDialog(window)
    window.jobs_dialog = queue
    queue.show()
    job = queue.store.enqueue("Sample", "sample", [
        {"name": "one", "seconds": 0.1},
        {"name": "two", "seconds": 0.1, "fail_once": True}])
    queue.refresh(job)
    ticks = []
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start()
    queue.start_queue()
    window.toggle_theme()
    wait_until(lambda: queue.worker is None and not queue.enabled)
    assert len(ticks) >= 5
    assert queue.store.jobs()[0]["state"] == "failed"
    queue.refresh(job)
    queue.retry_job()
    queue.start_queue()
    wait_until(lambda: queue.worker is None and not queue.enabled)
    timer.stop()
    assert [item["attempts"] for item in queue.store.items(job)] == [1, 2]


def test_close_cancels_worker_and_reopen_preserves_history(window):
    queue = QueueDialog(window)
    window.jobs_dialog = queue
    job = queue.store.enqueue("Long sample", "sample", [{"name": "long", "seconds": 4}])
    queue.start_queue()
    wait_until(lambda: queue.worker is not None and queue.store.items(job)[0]["state"] == "running")
    window.close()
    wait_until(lambda: queue.closed)
    assert queue.worker is None
    reopened = Store(queue.directory)
    assert reopened.items(job)[0]["state"] == "cancelled"
    reopened.close()


def test_copy_picker_preview_and_frozen_queue(window, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog, QDialog
    source = tmp_path / "original 日本.txt"
    window._tool_changed("Rename")
    source.write_text("copy me", encoding="utf-8")
    output = tmp_path / "copies"
    output.mkdir()
    window.start_scan([str(source)])
    wait_until(lambda: not window.busy)
    queue = QueueDialog(window)
    window.jobs_dialog = queue
    queue.show()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(output))
    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    queue.copy_current()
    wait_until(lambda: queue.plan_worker is None)
    assert len(queue.store.jobs()) == 1
    window.clear_files()
    queue.start_queue()
    wait_until(lambda: queue.worker is None and not queue.enabled)
    assert (output / source.name).read_bytes() == source.read_bytes()
    assert window.model.rowCount() == 0


def test_rejecting_copy_preview_does_not_queue_or_write(window, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog, QDialog
    source = tmp_path / "original.txt"
    window._tool_changed("Rename")
    source.write_text("keep")
    output = tmp_path / "copies"
    output.mkdir()
    window.start_scan([str(source)])
    wait_until(lambda: not window.busy)
    queue = QueueDialog(window)
    window.jobs_dialog = queue
    queue.show()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(output))
    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
    queue.copy_current()
    wait_until(lambda: queue.plan_worker is None)
    assert queue.store.jobs() == []
    assert list(output.iterdir()) == []


def test_replaced_output_folder_is_rejected(store, tmp_path):
    job, sources, output = copy_job(store, tmp_path)
    output.rename(tmp_path / "old output")
    output.mkdir()
    assert execute_job(store, job) == "failed"
    assert not list(output.iterdir())
    assert sources[0].read_bytes() == b"original data"


def test_recovery_preserves_edited_committed_file(store, tmp_path):
    job, sources, output = copy_job(store, tmp_path)
    class Crash(BaseException):
        pass
    def stop(stage):
        if stage == "committed":
            raise Crash()
    with pytest.raises(Crash):
        execute_job(store, job, hook=stop)
    target = output / sources[0].name
    target.write_bytes(b"user edited this output")
    store.recover()
    store.retry(job)
    assert execute_job(store, job) == "failed"
    assert target.read_bytes() == b"user edited this output"
    assert "Saved output changed" in store.items(job)[0]["message"]


def test_closing_while_copy_planning_stops_cleanly(window, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog
    source = tmp_path / "original.txt"
    source.write_text("keep")
    output = tmp_path / "copies"
    output.mkdir()
    window.start_scan([str(source)])
    wait_until(lambda: not window.busy)
    queue = QueueDialog(window)
    window.jobs_dialog = queue
    queue.show()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(output))
    queue.copy_current()
    window.close()
    wait_until(lambda: queue.closed)
    assert queue.plan_worker is None
    assert not list(output.iterdir())


@pytest.mark.parametrize("replace", [False, True])
def test_temporary_change_after_verification_is_not_committed(store, tmp_path, replace):
    job, _, output = copy_job(store, tmp_path)
    def tamper(stage):
        if stage == "prepared":
            item = store.items(job)[0]
            temp = Path(item["temp"])
            if replace:
                temp.rename(output / "saved old partial")
            temp.write_bytes(b"unrelated replacement content")
    assert execute_job(store, job, hook=tamper) == "failed"
    item = store.items(job)[0]
    assert not Path(json.loads(item["spec"])["output"]).exists()
    if replace:
        assert Path(item["temp"]).read_bytes() == b"unrelated replacement content"


def test_hidden_queue_shows_finished_results_when_reopened(window):
    queue = QueueDialog(window)
    window.jobs_dialog = queue
    queue.show()
    job = queue.store.enqueue("Background sample", "sample", [{"name": "one", "seconds": 0.15}])
    queue.refresh(job)
    queue.start_queue()
    queue.hide()
    wait_until(lambda: queue.worker is None and not queue.enabled)
    queue.show()
    assert queue.jobs_model.rows[0][1] == "succeeded"
    queue.tabs.setCurrentWidget(queue.views.history)
    queue.refresh(job)
    assert queue.items_model.rows[0][1] == "succeeded"


@pytest.mark.parametrize("cancel_during", ["running", "verifying"])
def test_cancel_mid_copy_or_mid_verification(store, tmp_path, cancel_during):
    job, sources, output = copy_job(store, tmp_path, content=b"x" * (3 * 1024 * 1024))
    cancel = threading.Event()
    def progress(item_id, done, total):
        if store.item(item_id)["state"] == cancel_during and done < total:
            cancel.set()
    assert execute_job(store, job, cancelled=cancel.is_set, progress=progress) == "cancelled"
    assert cancel.is_set()
    assert not list(output.iterdir())
    assert sources[0].stat().st_size == 3 * 1024 * 1024
    store.retry(job)
    assert execute_job(store, job) == "succeeded"
    assert (output / sources[0].name).read_bytes() == sources[0].read_bytes()
