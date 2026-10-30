from pathlib import Path

import pytest
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QFileDialog, QLabel
from media_tool.jobs_ui import QueueDialog
from media_tool import tool_paths
from test_foundation import app, window, wait_until


def prepare(window, tmp_path, width=4):
    source = tmp_path / "original.TXT"
    source.write_bytes(b"original content")
    window.start_scan([str(source)])
    wait_until(lambda: not window.busy)
    window._tool_changed("Rename")
    window.prefix.setText("Photo_")
    window.detail.setCurrentIndex(window.detail.findData(width))
    window.preview_changes()
    wait_until(lambda: not window.busy)
    return source


def widgets(layout):
    return [layout.itemAt(index).widget() for index in range(layout.count())
            if layout.itemAt(index).widget() is not None]


def test_feedback_toolbar_order_and_removed_banner(window):
    assert widgets(window.file_toolbar) == [window.add_button, window.recursive, window.remove_button,
                                           window.clear_button, window.columns_button]
    assert widgets(window.filter_row) == [window.name_filter, window.extension_filter,
                                         window.category_filter, window.reset_filters_button,window.visual.filters]
    assert window.jobs_button.text() == "Queue"
    assert window.recursive.text() == "Subfolders"
    assert not any("YOUR MEDIA" in label.text() for label in window.findChildren(QLabel))


def test_open_split_button_and_folder_menu(window, tmp_path, monkeypatch):
    source = tmp_path / "one.txt"
    source.write_text("one")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "two.txt").write_text("two")
    monkeypatch.setattr('media_tool.input_picker.pick_inputs',lambda *args:[str(source)])
    window.add_button.click()
    wait_until(lambda: not window.busy)
    assert window.model.rowCount() == 1
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(tmp_path))
    window.recursive.setChecked(True)
    window.open_folder_action.trigger()
    wait_until(lambda: not window.busy)
    assert window.model.rowCount() == 2


@pytest.mark.parametrize("width,expected", [(1, "Photo_1.TXT"), (2, "Photo_01.TXT"),
                                          (3, "Photo_001.TXT"), (4, "Photo_0001.TXT")])
def test_numbering_width_matches_preview(window, tmp_path, width, expected):
    source = prepare(window, tmp_path, width)
    assert Path(window.last_plan.items[0].output).name == expected
    assert source.read_bytes() == b"original content"
    assert window.start_rename_button.isEnabled()


def test_start_rename_does_not_open_or_drain_queue(window, tmp_path):
    source = prepare(window, tmp_path, 2)
    queue = QueueDialog(window)
    window.jobs_dialog = queue
    waiting = queue.store.enqueue("Waiting", "sample", [{"name": "wait", "seconds": 0}])
    queue.refresh()
    window.start_rename_button.click()
    window.start_rename_button.click()
    wait_until(lambda: queue.worker is None and not queue.enabled)
    assert not queue.isVisible()
    assert (tmp_path / "Photo_01.TXT").read_bytes() == b"original content"
    assert not source.exists()
    assert window.model.rowCount() == 0
    assert window.plan_model.rowCount() == 0
    assert Path(window.result_model.items[0]["output"]).name == "Photo_01.TXT"
    assert window.tabs.currentIndex() == 2
    assert queue.store.items(waiting)[0]["state"] == "queued"
    assert queue.store.items(waiting)[0]["attempts"] == 0
    assert len(queue.store.jobs()) == 2
    assert window.visual.activity.title.text()=='Succeeded'
    assert '1 completed' in window.visual.activity.counts.text()
    assert window.progress.value() == 100
    window.open_jobs()
    assert queue.isVisible()
    assert queue.store.items(waiting)[0]["attempts"] == 0


def test_direct_button_revalidates_changed_options_without_preview_click(window, tmp_path):
    source = prepare(window, tmp_path)
    window.prefix.setText("Changed_")
    assert window.start_rename_button.isEnabled()
    window.start_rename()
    wait_until(lambda: not window.busy and window.jobs_dialog.worker is None)
    assert not source.exists()
    assert (tmp_path / "Changed_0001.TXT").read_bytes() == b"original content"
    assert not (tmp_path / "Photo_0001.TXT").exists()


def test_direct_waits_for_running_job_then_becomes_available(window, tmp_path):
    source = prepare(window, tmp_path)
    queue = QueueDialog(window)
    window.jobs_dialog = queue
    queue.store.enqueue("Running sample", "sample", [{"name": "one", "seconds": 0.3}])
    queue.start_queue()
    assert not window.start_rename_button.isEnabled()
    assert window.run_button.isEnabled()
    window.start_rename()
    assert len(queue.store.jobs()) == 1
    assert source.exists()
    wait_until(lambda: queue.worker is None and not queue.enabled)
    assert window.start_rename_button.isEnabled()
    window.start_rename_button.click()
    wait_until(lambda: queue.worker is None and not queue.enabled)
    assert (tmp_path / "Photo_0001.TXT").exists()


def test_direct_failure_is_visible_without_queue_popup(window, tmp_path):
    source = prepare(window, tmp_path)
    source.write_bytes(b"changed after preview")
    window.start_rename_button.click()
    queue = window.jobs_dialog
    wait_until(lambda: queue.worker is None and not queue.enabled)
    assert not queue.isVisible()
    assert window.visual.activity.title.text()=='Failed'
    assert window.visual.activity.detail.text()
    assert source.read_bytes() == b"changed after preview"
    assert not (tmp_path / "Photo_0001.TXT").exists()
    assert window.model.rowCount() == 1
    assert window.result_model.items[0]["state"] == "failed"
    assert window.result_model.items[0]["details"]


def test_start_without_any_preview_and_keep_unselected_files(window, tmp_path):
    sources = [tmp_path / name for name in ("first.txt", "second.txt")]
    for source in sources:
        source.write_bytes(b"original")
    window.start_scan([str(source) for source in sources])
    wait_until(lambda: not window.busy)
    window._tool_changed("Rename")
    window.table.selectRow(0)
    assert window.last_plan is None
    assert window.start_rename_button.isEnabled()
    window.start_rename_button.click()
    window.start_rename_button.click()
    wait_until(lambda: not window.busy and window.jobs_dialog.worker is None)
    assert (tmp_path / "File_0001.txt").read_bytes() == b"original"
    assert not sources[0].exists()
    assert sources[1].exists()
    assert [record.path for record in window.model.items] == [str(sources[1])]
    assert window.plan_model.rowCount() == 0
    assert window.result_model.rowCount() == 1
    assert len(window.jobs_dialog.store.jobs()) == 1


def test_invalid_automatic_plan_does_not_rename_or_enqueue(window, tmp_path):
    source = prepare(window, tmp_path)
    window.prefix.setText("invalid/name")
    window.start_rename_button.click()
    wait_until(lambda: not window.busy)
    assert source.exists()
    assert window.jobs_dialog.store.jobs() == []
    assert window.last_plan is None
    assert "invalid filename" in window.status.text()
    assert window.result_model.rowCount() == 0
    assert not window._start_after_plan


def test_cancel_automatic_validation_never_runs(window, tmp_path):
    source = prepare(window, tmp_path)
    window.prefix.setText("Changed_")
    window.start_rename_button.click()
    window.cancel_scan()
    wait_until(lambda: not window.busy)
    assert source.exists()
    assert window.jobs_dialog.store.jobs() == []
    assert not window._start_after_plan


def test_pattern_skip_appears_in_results_without_an_output_name(window, tmp_path):
    first, second = tmp_path / "photo.png", tmp_path / "notes.txt"
    for source in (first, second):
        source.write_bytes(b"original")
    window.start_scan([str(first), str(second)])
    wait_until(lambda: not window.busy)
    window._tool_changed("Rename")
    window.rename_pattern.setText("*.png")
    window.start_rename_button.click()
    wait_until(lambda: not window.busy and window.jobs_dialog.worker is None)
    assert (tmp_path / "File_0001.png").exists()
    assert second.read_bytes() == b"original"
    assert window.model.rowCount() == 0
    skipped = next(index for index, item in enumerate(window.result_model.items) if item["state"] == "skipped")
    assert window.result_model.data(window.result_model.index(skipped, 1)) == "-"
    assert "pattern" in window.result_model.items[skipped]["details"]


def test_second_feedback_layout_and_hidden_rename_settings(window):
    window._tool_changed("Rename")
    assert [window.tabs.tabText(index) for index in range(window.tabs.count())] == ["Files", "Preview", "Results"]
    assert widgets(window.actions) == [window.cancel_button]
    assert window.issues_button in widgets(window.file_totals)
    assert window.start_rename_button.parent() is window.visual.inspector
    assert window.status not in widgets(window.actions)
    assert not window.destination.isVisible()
    assert not window.destination_label.isVisible()
    assert not window.browse_button.isVisible()
    assert not window.keep.isVisible()
    assert not window.phase_hint.isVisible()
    assert not any(label.text() == "TOOLS" or "Drop files or folders here" in label.text()
                   for label in window.findChildren(QLabel))
    assert all(not control.icon().isNull() for control in (*window.nav_group.buttons(), window.jobs_button))
    window._tool_changed("Images")
    assert window.destination.isVisible()
    assert window.keep.isVisible()


def test_scan_details_explains_its_scope(window, monkeypatch):
    shown = []
    monkeypatch.setattr(window, "_show_text", lambda title, text: shown.append((title, text)))
    window.issues.append("Skipped link: example")
    window.update_counts()
    window.show_issues()
    assert "while adding files and folders" in shown[0][1]
    assert "rename results" in shown[0][1]
    assert "Skipped link: example" in shown[0][1]
    assert window.issues_button.text() == "Scan details (1)"
    assert "last file scan" in window.issues_button.toolTip()


def test_seven_zip_registry_path_without_path_entry(tmp_path, monkeypatch):
    install = tmp_path / "custom install"
    install.mkdir()
    executable = install / "7z.exe"
    executable.write_bytes(b"test lookup only")
    monkeypatch.setattr(tool_paths.shutil, "which", lambda name: None)
    monkeypatch.setattr(tool_paths, "_seven_zip_registry", lambda: [install])
    assert tool_paths.resolve_tool("7z") == str(executable)


def test_seven_zip_path_entry_takes_priority(monkeypatch):
    monkeypatch.setattr(tool_paths.shutil, "which", lambda name: "/preferred/7z")
    monkeypatch.setattr(tool_paths, "_seven_zip_registry", lambda: pytest.fail("Registry should not be needed"))
    assert tool_paths.resolve_tool("7z") == "/preferred/7z"


def test_missing_explicit_executable_is_not_replaced_with_another_install(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_paths.shutil, "which", lambda name: None)
    monkeypatch.setattr(tool_paths, "_seven_zip_registry", lambda: pytest.fail("An explicit path should not fall back"))
    assert tool_paths.resolve_tool(str(tmp_path / "missing" / "7z.exe")) is None
