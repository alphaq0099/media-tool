import json
from pathlib import Path

import pytest

from media_tool.archive_cleanup import cleanup_archive
from media_tool.domain import FileRecord
from media_tool.jobs import Store, execute_job
from media_tool.planning import PlanRequest
from media_tool.zip_plan import plan_archives, archive_snapshot
from test_foundation import app, window, wait_until


def saved_archive(tmp_path):
    folder = tmp_path / "inputs"
    folder.mkdir()
    (folder / "one.txt").write_text("one source")
    (folder / "two.txt").write_text("two source")
    (folder / "excluded.tmp").write_text("excluded source")
    plan = plan_archives([FileRecord.from_path(folder)], PlanRequest("archives", archive_exclusions="*.tmp"))
    store = Store(tmp_path / "history")
    job = store.enqueue("ZIP", "archives", archive_snapshot(plan))
    execute_job(store, job)
    item = store.items(job)[0]
    assert item["state"] == "succeeded", item["message"]
    return folder, store, job, item


def fake_trash(tmp_path, monkeypatch, fail=None):
    trash = tmp_path / "trash"
    trash.mkdir()
    def move(path):
        if path.name == fail:
            raise OSError("Trash is unavailable for this file")
        target = trash / path.name
        path.rename(target)
        return str(target)
    monkeypatch.setattr("media_tool.archive_cleanup.move_to_trash", move)
    return trash


def test_archive_cleanup_only_verified_included_files_is_idempotent(tmp_path, monkeypatch):
    folder, store, job, item = saved_archive(tmp_path)
    trash = fake_trash(tmp_path, monkeypatch)
    try:
        result = cleanup_archive(store, item["id"])
        assert (result["moved"], result["kept"]) == (2, 0)
        assert folder.exists() and (folder / "excluded.tmp").read_text() == "excluded source"
        assert sorted(p.name for p in trash.iterdir()) == ["one.txt", "two.txt"]
        assert cleanup_archive(store, item["id"])["moved"] == 2
        assert store.item(item["id"])["state"] == "succeeded"
    finally:
        store.close()


@pytest.mark.parametrize("failure", ["partial_trash", "trash_unavailable", "changed_source", "pending_job", "cancelled", "changed_output", "incomplete", "late_change", "late_output"])
def test_cleanup_failure_keeps_sources_and_archive_success(tmp_path, monkeypatch, failure):
    folder, store, job, item = saved_archive(tmp_path)
    fake_trash(tmp_path, monkeypatch, fail="two.txt" if failure == "partial_trash" else None)
    if failure == "trash_unavailable":
        def unavailable(path):
            raise OSError("Recycle Bin / Trash is unavailable")
        monkeypatch.setattr("media_tool.archive_cleanup.move_to_trash", unavailable)
    try:
        if failure == "changed_source":
            (folder / "one.txt").write_text("edited after archive")
        elif failure == "pending_job":
            store.enqueue("Waiting", "copy", [dict(source=str(folder / "one.txt"), identity=json.loads(item["spec"])["manifest"]["entries"][1]["identity"])])
        elif failure == "changed_output":
            Path(json.loads(item["spec"])["output"]).write_bytes(b"user changed archive")
        elif failure == "incomplete":
            store.set_item(item["id"], "failed", "interrupted verification")
        if failure in ("changed_output", "incomplete"):
            with pytest.raises(ValueError):
                cleanup_archive(store, item["id"])
            assert (folder / "one.txt").exists() and (folder / "two.txt").exists()
        else:
            def hook(point):
                if point == "archive_cleanup_prepared" and failure == "late_change":
                    (folder / "one.txt").write_text("late change")
                elif point == "archive_cleanup_prepared" and failure == "late_output":
                    Path(json.loads(item["spec"])["output"]).write_bytes(b"archive edited during cleanup")
            result = cleanup_archive(store, item["id"], cancelled=lambda: failure == "cancelled", hook=hook)
            if failure in ("cancelled", "trash_unavailable", "late_output"):
                assert result["moved"] == 0 and result["kept"] == 2
            else:
                assert result["moved"] == result["kept"] == 1
            assert store.item(item["id"])["state"] == "succeeded"
        assert (folder / "excluded.tmp").exists()
    finally:
        store.close()


def test_cleanup_interruption_after_trash_does_not_move_replacement(tmp_path, monkeypatch):
    folder, store, job, item = saved_archive(tmp_path)
    fake_trash(tmp_path, monkeypatch)
    try:
        class SimulatedCrash(BaseException):
            pass
        with pytest.raises(SimulatedCrash):
            cleanup_archive(store, item["id"], hook=lambda point: (_ for _ in ()).throw(SimulatedCrash()) if point == "archive_source_trashed" else None)
        (folder / "one.txt").write_text("new unrelated file")
        cleanup_archive(store, item["id"])
        assert (folder / "one.txt").read_text() == "new unrelated file"
        assert store.item(item["id"])["state"] == "succeeded"
    finally:
        store.close()


def test_ui_cleanup_review_and_worker_summary(window, tmp_path, monkeypatch):
    folder = tmp_path / "pages"
    folder.mkdir()
    (folder / "one.txt").write_text("generated source")
    fake_trash(tmp_path, monkeypatch)
    window._tool_changed("Archives")
    window.format.setCurrentText("ZIP")
    window.start_scan([str(folder)])
    wait_until(lambda: not window.busy)
    window.start_rename_button.click()
    wait_until(lambda: not window.busy and window.jobs_dialog is not None and not window.jobs_dialog.enabled, timeout=30000)
    assert window.archive_cleanup_button.isVisible()
    window.archive_cleanup_button.click()
    dialog = window.archive_cleanup_dialog
    from PySide6.QtWidgets import QPushButton
    button = next(b for b in dialog.findChildren(QPushButton) if b.text().startswith("Move verified"))
    assert (folder / "one.txt").exists()
    button.click()
    wait_until(lambda: not window.busy, timeout=30000)
    assert not (folder / "one.txt").exists()
    assert "1 moved" in window.result_model.items[0]["details"]
    assert window.result_model.items[0]["state"] == "succeeded"


def test_saved_archive_results_reopen_after_history_session_restart(window, tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("saved source")
    window._tool_changed("Archives")
    window.format.setCurrentText("ZIP")
    window.start_scan([str(source)])
    wait_until(lambda: not window.busy)
    window.start_rename_button.click()
    wait_until(lambda: not window.busy and window.jobs_dialog is not None and not window.jobs_dialog.enabled, timeout=30000)
    job_id = window.last_result_job
    assert window.jobs_dialog.shutdown()
    window.jobs_dialog = None
    window.last_result_job = None
    window._tool_changed("Images")
    queue = window.ensure_jobs()
    queue.refresh(job_id)
    assert queue.archive_results_button.isEnabled()
    queue.archive_results_button.click()
    assert window.tool == "Archives" and window.last_result_job == job_id
    assert window.result_model.items[0]["state"] == "succeeded"
    assert window.archive_cleanup_button.isVisible()
    window.archive_cleanup_button.click()
    assert window.archive_cleanup_dialog.isVisible() and source.exists()
    window.archive_cleanup_dialog.close()
