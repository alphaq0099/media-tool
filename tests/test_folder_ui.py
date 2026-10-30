import json
from pathlib import Path
import threading

import pytest
from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtTest import QTest

from media_tool.folder_ui import CheckCaption, FolderTreeModel, FolderWorker
from media_tool.folders import folder_view, scan_folder
from media_tool.jobs import execute_job
from test_foundation import app, window, wait_until
from test_folders import tree_fixture, files


def page_for(window, root):
    window._tool_changed("Folders")
    page = window.folders_page
    assert page.open_paths([str(root)])
    wait_until(lambda: not window.busy, 15000)
    return page


def test_folder_page_is_lazy_and_preserves_other_tool_state(window):
    assert window.folders_page is None
    assert window.jobs_dialog is None
    window._tool_changed("Rename")
    window.prefix.setText("remember")
    window._tool_changed("Folders")
    assert window.folders_active
    assert window.jobs_dialog is None
    window._tool_changed("Rename")
    assert not window.folders_active
    assert window.prefix.text() == "remember"


def test_scan_filter_and_preview_never_modify_source(window, tmp_path):
    root = tree_fixture(tmp_path)
    before = files(root)
    page = page_for(window, root)
    assert page.snapshot.complete
    assert "18 B total" in page.summary.text()
    assert window.jobs_dialog is None
    page.minimum.setValue(0.001)
    wait_until(lambda: not page.filter_timer.isActive() and not window.busy)
    assert page.tree_model.view.matching_files[0] == 0
    page.operation.setCurrentIndex(1)
    page.preview()
    wait_until(lambda: not window.busy, 15000)
    assert page.plan.can_apply
    assert page.tabs.currentIndex() == 1
    assert page.before_model.view is not None and page.after_model.view is not None
    assert page.run_button.isEnabled()
    assert files(root) == before
    assert window.jobs_dialog is None


def test_ui_apply_undo_and_saved_queue_undo(window, tmp_path):
    root = tree_fixture(tmp_path)
    before = files(root)
    page = page_for(window, root)
    page.operation.setCurrentIndex(1)
    page.loose.setChecked(True)
    page.preview()
    wait_until(lambda: not window.busy, 15000)
    page.apply()
    wait_until(lambda: not window.busy, 15000)
    queue = window.jobs_dialog
    job = page.result_job
    assert page.result_undoable
    assert page.tabs.currentIndex() == 2
    assert page.snapshot is None
    assert not (root / "ParentA").exists()
    # Reach undo using saved Queue rather than relying on an in-memory plan.
    queue.refresh(job)
    assert queue.undo_button.isEnabled()
    queue.preview_selected_undo()
    wait_until(lambda: not window.busy, 15000)
    assert page.plan.can_apply and page.plan.original_job == job
    assert page.run_button.text() == "Apply undo"
    page.apply()
    wait_until(lambda: not window.busy, 15000)
    assert files(root) == before
    assert not page.result_undoable
    assert "restored" in page.status.text()


def test_invalid_filter_range_does_not_enable_flatten(window, tmp_path):
    page = page_for(window, tree_fixture(tmp_path))
    page.minimum.setValue(2)
    page.maximum.setValue(1)
    wait_until(lambda: not page.filter_timer.isActive() and not window.busy)
    assert "Maximum size" in page.status.text()
    assert page.plan is None and not page.run_button.isEnabled()


def test_selected_scope_rejects_nested_and_root_selections(window, tmp_path):
    root = tree_fixture(tmp_path)
    page = page_for(window, root)
    page.operation.setCurrentIndex(1)
    page.scope.setCurrentIndex(1)
    page.tree.setCurrentIndex(page.tree_model.index(0, 0))
    page.preview()
    wait_until(lambda: not window.busy, 15000)
    assert page.plan is None
    assert "immediate" in page.status.text()


def test_root_edit_invalidates_cached_tree_and_plan(window, tmp_path):
    page = page_for(window, tree_fixture(tmp_path))
    page.operation.setCurrentIndex(1)
    page.preview()
    wait_until(lambda: not window.busy, 15000)
    page.root.textEdited.emit("new root")
    assert page.snapshot is None and page.plan is None
    assert page.tree_model.rowCount() == 0
    assert not page.run_button.isEnabled()


def test_tree_rows_are_lazy_and_model_parent_indexes_are_consistent(app, tmp_path):
    root = tmp_path / "root"
    (root / "folder").mkdir(parents=True)
    for number in range(600):
        (root / "folder" / f"file{number}").write_bytes(b"x")
    model = FolderTreeModel()
    model.set_view(folder_view(scan_folder(root)))
    top = model.index(0, 0)
    folder = model.index(0, 0, top)
    assert model.rowCount(folder) == 0 and model.canFetchMore(folder)
    assert model.parent(folder) == top
    model.fetchMore(folder)
    assert model.rowCount(folder) == 256
    item = model.index(200, 0, folder)
    assert model.parent(item) == folder
    assert model.rowCount(model.index(0, 1, top)) == 0
    while model.canFetchMore(folder):
        model.fetchMore(folder)
    assert model.rowCount(folder) == 600


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("size", [(1180, 740), (880, 610)])
def test_folder_layout_fits_window_and_theme(window, tmp_path, theme, size):
    window.resize(*size)
    window.set_theme(theme)
    page = page_for(window, tree_fixture(tmp_path))
    page.operation.setCurrentIndex(1)
    QTest.qWait(30)
    assert window.size().width() == size[0]
    assert window.size().height() == size[1]
    assert page.run_button.isVisible()
    assert page.run_button.mapTo(page, page.run_button.rect().bottomRight()).y() < page.height()
    assert page.summary.mapTo(page, page.summary.rect().bottomLeft()).y() < page.height()
    assert window.theme_button.isVisible()
    assert page.tree.width() > 200


def test_close_cancels_folder_worker_without_destroying_live_thread(window, tmp_path):
    page = page_for(window, tree_fixture(tmp_path))
    def work(cancelled, progress):
        while not cancelled():
            threading.Event().wait(0.005)
        return None
    assert page.launch("filter", work, "Testing cooperative shutdown")
    assert window.busy
    window.close()
    wait_until(lambda: not window.isVisible() and not window.busy, 10000)


def test_flatten_refreshes_existing_intake_paths_without_losing_identity(window, tmp_path):
    root = tree_fixture(tmp_path)
    source = root / "ParentB" / "Other" / "c.txt"
    window.start_scan([str(source)])
    wait_until(lambda: not window.busy)
    record = window.model.items[0]
    page = page_for(window, root)
    page.operation.setCurrentIndex(1)
    page.preview()
    wait_until(lambda: not window.busy, 15000)
    page.apply()
    wait_until(lambda: not window.busy, 15000)
    assert window.model.items[0].path == str(root / "Other" / "c.txt")
    assert window.model.items[0].inode == record.inode


def test_queue_adds_frozen_folder_plan_without_moving_inputs(window, tmp_path):
    root = tree_fixture(tmp_path)
    before = files(root)
    page = page_for(window, root)
    page.operation.setCurrentIndex(1)
    page.preview()
    wait_until(lambda: not window.busy, 15000)
    page.apply(queued=True)
    queue = window.jobs_dialog
    job = page.result_job
    assert files(root) == before
    assert next(j for j in queue.store.jobs() if j["id"] == job)["state"] == "queued"
    page.loose.setChecked(True)
    assert all(json.loads(i["spec"])["remove_empty"] for i in queue.store.items(job) if i["state"] == "queued")
    assert all(json.loads(i["spec"])["kind"] == "Folder" for i in queue.store.items(job) if i["state"] == "queued")
    assert sum(i["state"] == "skipped" for i in queue.store.items(job)) == 1
    queue.start_single(job)
    wait_until(lambda: not window.busy, 15000)
    assert (root / "ParentA" / "loose.txt").read_bytes() == b"xy"
    assert (root / "Other" / "c.txt").read_bytes() == b"12345"
    queue.refresh(job)
    assert queue.archive_results_button.text() == "Folder results"
    assert queue.archive_results_button.isEnabled()
    queue.show_archive_results()
    assert page.tabs.currentIndex() == 2


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_folder_settings_fit_scroll_viewport_in_both_modes(window, tmp_path, theme):
    window.set_theme(theme)
    page = page_for(window, tree_fixture(tmp_path))
    for mode in (0, 1):
        page.operation.setCurrentIndex(mode)
        QTest.qWait(30)
        inner = page.settings_scroll.widget()
        assert inner.width() <= page.settings_scroll.viewport().width()
        for control in (page.operation, page.minimum, page.maximum, page.depth, page.scope, page.collision):
            if control.isVisible():
                assert control.width() <= page.settings_scroll.viewport().width()


def test_wrapped_checkbox_caption_toggles_control_and_has_accessible_name(window, tmp_path):
    page = page_for(window, tree_fixture(tmp_path))
    page.operation.setCurrentIndex(1)
    caption = page.loose.parentWidget().findChild(CheckCaption)
    assert not page.loose.isChecked()
    assert page.loose.accessibleName() == "Move loose files too"
    QTest.mouseClick(caption, Qt.MouseButton.LeftButton)
    assert page.loose.isChecked()


def test_active_media_job_can_be_cancelled_from_folders_page(window, tmp_path, monkeypatch):
    from media_tool.domain import FileRecord
    from media_tool.jobs import Cancelled, copy_snapshot
    started = threading.Event()
    def waiting_image(store, item, cancelled, progress, hook):
        started.set()
        while not cancelled():
            threading.Event().wait(0.005)
        raise Cancelled("Cancelled fixture processing; original kept.")
    monkeypatch.setattr("media_tool.images.run_image", waiting_image)
    source = tmp_path / "source.png"
    source.write_bytes(b"disposable fixture; decoder replaced by cancellable worker")
    specs = copy_snapshot([FileRecord.from_path(source)], tmp_path)
    page = window.show_folders()
    queue = window.ensure_jobs()
    job = queue.store.enqueue("Existing media job", "images", specs)
    queue.start_single(job)
    wait_until(started.is_set)
    assert window.busy and page.cancel_button.isEnabled()
    page.cancel()
    wait_until(lambda: not window.busy, 10000)
    assert queue.store.items(job)[0]["state"] == "cancelled"
    assert source.read_bytes().startswith(b"disposable fixture")
    assert "incomplete" in page.status.text()
