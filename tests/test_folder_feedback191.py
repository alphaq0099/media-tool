"""Behavior checks for folder feedback: editing, ranges, sorting and clear."""
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QStyle, QStyleOptionSpinBox

from media_tool.folder_ui import FolderTreeModel
from media_tool.folders import folder_view, scan_folder
from test_foundation import app, window, wait_until
from test_folder_ui import page_for
from test_folders import files, tree_fixture


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_numeric_edit_from_empty_area_and_spin_buttons(window, tmp_path, theme):
    window.set_theme(theme)
    page = page_for(window, tree_fixture(tmp_path))
    page.root.setFocus()
    editor = page.minimum.lineEdit()
    QTest.mouseClick(editor, Qt.MouseButton.LeftButton, pos=QPoint(editor.width() - 4, editor.height() // 2))
    QTest.qWait(10)
    QTest.keyClicks(editor, "12.5")
    assert page.minimum.value() == 12.5
    assert page.minimum.suffix() == page.maximum.suffix() == ""
    page.filter_timer.stop()
    for control in (page.minimum, page.depth):
        option = QStyleOptionSpinBox()
        control.initStyleOption(option)
        before = control.value()
        up = control.style().subControlRect(QStyle.ComplexControl.CC_SpinBox, option,
                                           QStyle.SubControl.SC_SpinBoxUp, control)
        down = control.style().subControlRect(QStyle.ComplexControl.CC_SpinBox, option,
                                             QStyle.SubControl.SC_SpinBoxDown, control)
        assert up.width() >= 20 and down.height() > 0
        QTest.mouseClick(control, Qt.MouseButton.LeftButton, pos=up.center())
        assert control.value() > before
        QTest.mouseClick(control, Qt.MouseButton.LeftButton, pos=down.center())
        assert control.value() == before
    if theme == "dark":
        for control in (page.minimum, page.depth):
            control.setValue(control.value()+1)  # Both arrow directions enabled.
            option=QStyleOptionSpinBox()
            control.initStyleOption(option)
            image=control.grab().toImage()
            scale=image.devicePixelRatio()
            for part in (QStyle.SubControl.SC_SpinBoxUp,QStyle.SubControl.SC_SpinBoxDown):
                center=control.style().subControlRect(QStyle.ComplexControl.CC_SpinBox,option,part,control).center()
                bright=sum(min(image.pixelColor(round((center.x()+x)*scale),round((center.y()+y)*scale)).getRgb()[:3])>200
                           for x in range(-5,6) for y in range(-5,6))
                assert bright>0,'Dark-mode spinner arrow is not visible.'


def test_invalid_range_warning_is_immediate_and_zero_means_unlimited(window):
    page = window.show_folders()
    page.minimum.setValue(5)
    page.maximum.setValue(2)
    assert page.size_warning.isVisible()
    assert not page.filter_timer.isActive() and page.worker is None
    assert page.minimum.value() == 5 and page.maximum.value() == 2
    page.maximum.setValue(5)
    assert not page.size_warning.isVisible()
    page.maximum.setValue(0)
    assert page.range_valid()
    assert page.options()["maximum"] is None


def test_inspector_filters_do_not_apply_to_flatten_and_are_labelled(window, tmp_path):
    root = tree_fixture(tmp_path)
    original = files(root)
    page = page_for(window, root)
    page.minimum.setValue(5)
    page.maximum.setValue(2)
    assert page.size_warning.isVisible()
    page.operation.setCurrentIndex(1)
    assert not page.size_panel.isVisible()
    assert not hasattr(page, "size_scope_hint")
    assert page.options()["minimum"] == 0 and page.options()["maximum"] is None
    page.preview()
    wait_until(lambda: not window.busy, 15000)
    assert page.plan.can_apply
    assert sum(i.status == "Ready" for i in page.plan.items) == 3
    assert page.tree_model.view.min_bytes == 0
    assert page.tree_model.view.matching_files[0] == 5
    assert files(root) == original
    page.operation.setCurrentIndex(0)
    assert page.minimum.value() == 5 and page.maximum.value() == 2
    assert page.size_warning.isVisible()


def test_column_click_sorting_uses_cache_and_reverses(window, tmp_path, monkeypatch):
    page = page_for(window, tree_fixture(tmp_path))
    def no_scan(*args):
        pytest.fail("Column sorting must not rescan the filesystem")
    monkeypatch.setattr("media_tool.folders.os.scandir", no_scan)
    def click(column):
        header = page.tree.header()
        page.tree.horizontalScrollBar().setValue(0 if column < 3 else page.tree.horizontalScrollBar().maximum())
        x = header.sectionViewportPosition(column) + header.sectionSize(column) // 2
        QTest.mouseClick(header.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(x, header.height() // 2))
        wait_until(lambda: not page.filter_timer.isActive() and not window.busy, 10000)
    def names():
        root = page.tree_model.index(0, 0)
        return [page.tree_model.index(row, 0, root).data() for row in range(page.tree_model.rowCount(root))]
    click(0)
    assert names()[:3] == ["ParentB", "ParentA", "empty"]
    click(1)
    assert names()[0] == "ParentA"
    click(1)
    assert names()[0] == "empty"
    click(2)
    assert names()[0] == "ParentA"
    click(4)
    assert names()[0] == "root.txt"


def test_size_column_and_counts_have_no_duplicate_ratios(app, tmp_path):
    snapshot = scan_folder(tree_fixture(tmp_path))
    model = FolderTreeModel()
    model.set_view(folder_view(snapshot))
    assert model.columnCount() == 5
    assert model.headerData(1, Qt.Orientation.Horizontal) == "Total size"
    assert model.index(0, 2).data() == "5"
    model.set_view(folder_view(snapshot, minimum=3, maximum=6))
    assert model.headerData(1, Qt.Orientation.Horizontal) == "Size (filtered)"
    assert model.index(0, 1).data() == "8 B"
    assert model.index(0, 2).data() == "2"
    tooltip = model.data(model.index(0, 1), Qt.ItemDataRole.ToolTipRole)
    assert "18 bytes" in tooltip and "8 bytes" in tooltip


def test_clear_keeps_files_and_saved_jobs_and_rows_are_taller(window, tmp_path):
    root = tree_fixture(tmp_path)
    original = files(root)
    page = page_for(window, root)
    assert page.tree.visualRect(page.tree_model.index(0, 0)).height() >= 30
    page.operation.setCurrentIndex(1)
    page.preview()
    wait_until(lambda: not window.busy, 15000)
    page.apply(queued=True)
    job = page.result_job
    QTest.mouseClick(page.clear_button, Qt.MouseButton.LeftButton)
    assert page.snapshot is None and page.plan is None and page.root.text() == ""
    assert page.tree_model.rowCount() == 0 and page.moves_model.rowCount() == 0
    assert window.jobs_dialog.store.items(job)
    assert files(root) == original


def test_partial_scan_still_exposes_issue_details_after_clear_button_replacement(window, tmp_path, monkeypatch):
    import os
    root = tree_fixture(tmp_path)
    original_scan = os.scandir
    def denied(path):
        if Path(path).name == "Child2":
            raise PermissionError("feedback fixture permission failure")
        return original_scan(path)
    monkeypatch.setattr(os, "scandir", denied)
    page = page_for(window, root)
    assert not page.snapshot.complete
    assert page.issues_button.isVisible()
    messages = []
    monkeypatch.setattr(window, "_show_text", lambda title, text: messages.append(text))
    QTest.mouseClick(page.issues_button, Qt.MouseButton.LeftButton)
    assert "feedback fixture permission failure" in messages[0]
