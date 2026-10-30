"""Phase-1 output planning and real UI behavior, using disposable fixtures."""
import hashlib
import os
from pathlib import Path

import pytest
from PySide6.QtCore import QItemSelectionModel, QTimer
from PySide6.QtWidgets import QFileDialog

from test_foundation import app, window, wait_until
from media_tool.domain import FileRecord, FilterSpec
from media_tool.planning import PlanRequest, build_plan, validate_name, PlanCancelled


def make_record(path, content=b"fixture only"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return FileRecord.from_path(path)


def manifest(root):
    return {str(path.relative_to(root)): (hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_mtime_ns) for path in root.rglob("*") if path.is_file()}


def test_deterministic_plan_is_read_only_and_does_not_create_destination(tmp_path):
    records = [make_record(tmp_path / "b" / "image2.png"),
               make_record(tmp_path / "a" / "image10.webp")]
    before = manifest(tmp_path)
    destination = tmp_path / "not-created" / "outputs"
    request = PlanRequest("images", str(destination), "jpg")
    first = build_plan(records, request)
    second = build_plan(tuple(reversed(records)), request)
    assert first == second
    assert first.counts == {"Planned": 2}
    assert not destination.exists()
    assert before == manifest(tmp_path)


def test_batch_and_existing_name_collisions_reserved_together(tmp_path):
    records = [make_record(tmp_path / "a" / "same.png"),
               make_record(tmp_path / "b" / "same.webp")]
    destination = tmp_path / "out"
    destination.mkdir()
    (destination / "same.jpg").write_bytes(b"existing output")
    request = PlanRequest("images", str(destination), "jpg")
    plan = build_plan(records, request)
    assert {Path(i.output).name for i in plan.items} == {"same_1.jpg", "same_2.jpg"}
    assert (destination / "same.jpg").read_bytes() == b"existing output"
    skipped = build_plan(records, PlanRequest("images", str(destination), "jpg", "skip"))
    assert skipped.counts == {"Skipped": 2}


def test_source_output_identity_never_overwrites_input(tmp_path):
    source = make_record(tmp_path / "same.jpg")
    plan = build_plan([source], PlanRequest("images", str(tmp_path), "jpg"))
    assert Path(plan.items[0].output).name == "same_1.jpg"
    assert Path(source.path).read_bytes() == b"fixture only"


def test_incompatible_inputs_are_reported(tmp_path):
    records = [make_record(tmp_path / "image.jpg"), make_record(tmp_path / "document.txt")]
    plan = build_plan(records, PlanRequest("video", str(tmp_path / "out"), "mp4"))
    assert plan.counts == {"Incompatible": 2}


def test_stale_and_missing_sources_cannot_be_planned(tmp_path):
    changed = make_record(tmp_path / "changed.png")
    deleted = make_record(tmp_path / "missing.png")
    Path(changed.path).write_bytes(b"new longer data")
    Path(deleted.path).unlink()
    plan = build_plan([changed, deleted], PlanRequest("images", str(tmp_path), "jpg"))
    assert plan.counts == {"Invalid": 2}
    assert any("changed since discovery" in i.reason for i in plan.items)


def test_replaced_source_with_same_size_and_timestamp_is_detected(tmp_path):
    old = make_record(tmp_path / "old.png")
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"x" * old.size)
    try:
        os.utime(replacement, ns=(old.modified_ns, old.modified_ns))
        os.replace(replacement, old.path)
    except OSError:
        pytest.skip("Host cannot replace the fixture")
    current = Path(old.path).stat()
    if not old.inode or current.st_ino == old.inode:
        pytest.skip("Filesystem does not expose replacement identity")
    plan = build_plan([old], PlanRequest("images", str(tmp_path), "jpg"))
    assert plan.items[0].status == "Invalid"


def test_rename_preview_natural_order_unchanged_and_collisions(tmp_path):
    records = [make_record(tmp_path / "z" / "file10.JPG"),
               make_record(tmp_path / "z" / "file2.png")]
    request = PlanRequest("rename", prefix="Photo_", start=7, padding=3)
    plan = build_plan(records, request)
    assert [(i.source.name, Path(i.output).name) for i in plan.items] == [
        ("file2.png", "Photo_007.png"), ("file10.JPG", "Photo_008.JPG")]
    unchanged = make_record(tmp_path / "exact" / "Photo_0001.jpg")
    assert build_plan([unchanged], PlanRequest("rename", prefix="Photo_")).items[0].status == "Unchanged"


@pytest.mark.parametrize("name", ["CON.jpg", "aux.txt", "LPT1.png", "bad:name.jpg",
    "../escape.jpg", "x\\y.jpg", "trailing. ", "", "..", "x" * 256 + ".jpg"])
def test_portable_filename_validation(name):
    assert validate_name(name)


@pytest.mark.parametrize("options", [
    PlanRequest("images", "relative/path"),
    PlanRequest("video", "relative/path", "mp4"),
    PlanRequest("images", "/tmp", "not-a-format"),
    PlanRequest("rename", prefix="../"),
    PlanRequest("rename", start=0),
    PlanRequest("rename", padding=0),
])
def test_invalid_requests_are_actionable(options):
    with pytest.raises(ValueError):
        build_plan([], options)


def test_output_parent_file_and_denied_access_reported(tmp_path, monkeypatch):
    source = make_record(tmp_path / "source.jpg")
    plain_file = tmp_path / "file"
    plain_file.write_text("not a folder")
    with pytest.raises(ValueError, match="not a folder"):
        build_plan([source], PlanRequest("images", str(plain_file / "out")))
    original = Path.stat
    denied = tmp_path / "denied"
    def fake_stat(path, *args, **kwargs):
        if path == denied:
            raise PermissionError("fixture permission denied")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "stat", fake_stat)
    with pytest.raises(ValueError, match="Cannot inspect output"):
        build_plan([source], PlanRequest("images", str(denied)))


def test_filters_compose_case_insensitively():
    row = FileRecord("sample://1", "Holiday [01].PNG", 10, "Image", True)
    assert FilterSpec.parse("Image", "HOLIDAY [", "*.png, jpg").matches(row)
    assert not FilterSpec.parse("Video", "", "").matches(row)
    assert not FilterSpec.parse("Image", "other", "png").matches(row)


def test_duplicate_source_and_sample_are_explicit(tmp_path):
    source = make_record(tmp_path / "source.png")
    plan = build_plan([source, source], PlanRequest("images", str(tmp_path), "jpg"))
    assert plan.counts == {"Planned": 1, "Skipped": 1}
    sample = FileRecord("sample://1", "sample.png", 1, "Image", True)
    plan = build_plan([sample], PlanRequest("rename"))
    assert plan.items[0].status == "Sample"
    assert not (tmp_path / "File_0001.png").exists()


def test_plan_cancellation_does_not_mutate_sources(tmp_path):
    records = [make_record(tmp_path / "a.jpg")]
    before = manifest(tmp_path)
    with pytest.raises(PlanCancelled):
        build_plan(records, PlanRequest("rename"), cancelled=lambda: True)
    assert before == manifest(tmp_path)


def test_ui_filter_counts_selection_and_removal_are_consistent(window, tmp_path):
    records = [make_record(tmp_path / "photo1.png"), make_record(tmp_path / "photo2.JPG"),
               make_record(tmp_path / "movie.mp4"), make_record(tmp_path / "notes.txt")]
    window.start_scan([r.path for r in records])
    wait_until(lambda: not window.busy)
    window.category_filter.setCurrentText("All images")
    window.name_filter.setText("PHOTO")
    window.extension_filter.setText("jpg, png")
    window.apply_filters()
    assert window.proxy.rowCount() == 2
    assert "2 visible / 2 total" in window.count_label.text()
    window.table.selectRow(0)
    selected_path = window.model.items[window.proxy.mapToSource(
        window.table.selectionModel().selectedRows()[0]).row()].path
    window.remove_selected()
    assert window.model.rowCount() == 3
    assert window.proxy.rowCount() == 1
    assert Path(selected_path).exists()
    assert "1 visible / 1 total" in window.count_label.text()


def test_ui_preview_scope_and_invalidation(window, tmp_path):
    from PIL import Image
    for name in ("a.png", "b.png"):
        Image.new("RGB", (4, 4)).save(tmp_path / name)
    records = [FileRecord.from_path(tmp_path / "a.png"), FileRecord.from_path(tmp_path / "b.png"),
               make_record(tmp_path / "c.mp4")]
    window.start_scan([r.path for r in records])
    wait_until(lambda: not window.busy)
    (tmp_path / "outputs").mkdir()
    window.destination.setText(str(tmp_path / "outputs"))
    window.category_filter.setCurrentText("All images")
    window.table.selectRow(1)
    assert window.preview_changes()
    wait_until(lambda: not window.busy)
    assert len(window.last_plan.items) == 1
    assert window.last_plan.items[0].source.name == "b.png"
    assert window.last_plan.counts == {"Planned": 1}
    assert not list((tmp_path / "outputs").iterdir())
    window.format.setCurrentText("PNG")
    assert window.last_plan is None
    assert window.plan_model.rowCount() == 0
    window.table.clearSelection()
    window.preview_changes()
    wait_until(lambda: not window.busy)
    assert len(window.last_plan.items) == 2


def test_ui_preview_repeated_same_results(window, tmp_path):
    from PIL import Image
    Image.new("RGB", (4, 4)).save(tmp_path / "image.png")
    row = FileRecord.from_path(tmp_path / "image.png")
    window.start_scan([row.path])
    wait_until(lambda: not window.busy)
    (tmp_path / "out").mkdir()
    window.destination.setText(str(tmp_path / "out"))
    window.preview_changes()
    wait_until(lambda: not window.busy)
    first = window.last_plan
    window.preview_changes()
    wait_until(lambda: not window.busy)
    assert first == window.last_plan


def test_native_output_picker_does_not_create_folder(window, monkeypatch, tmp_path):
    destination = str(tmp_path / "nonexistent")
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: destination)
    window.choose_destination()
    assert window.destination.text() == destination
    assert not Path(destination).exists()


def test_scan_skips_nested_output_but_accepts_explicit_file(window, tmp_path):
    source = make_record(tmp_path / "input.png")
    old_output = make_record(tmp_path / "output" / "old.jpg")
    window.destination.setText(str(tmp_path / "output"))
    window.recursive.setChecked(True)
    window.start_scan([str(tmp_path)])
    wait_until(lambda: not window.busy)
    assert [r.name for r in window.model.items] == ["input.png"]
    assert any("Skipped output folder" in notice for notice in window.issues)
    window.start_scan([old_output.path])
    wait_until(lambda: not window.busy)
    assert window.model.rowCount() == 2


def test_scan_reports_permission_failure_without_losing_other_inputs(window, monkeypatch, tmp_path):
    import media_tool.services as services
    source = make_record(tmp_path / "good" / "input.png")
    denied = tmp_path / "denied"
    denied.mkdir()
    original = services.os.scandir
    def fake_scandir(path):
        if Path(path) == denied:
            raise PermissionError("fixture permission denied")
        return original(path)
    monkeypatch.setattr(services.os, "scandir", fake_scandir)
    window.start_scan([str(denied), source.path])
    wait_until(lambda: not window.busy)
    assert window.model.rowCount() == 1
    assert any("permission denied" in notice for notice in window.issues)


def test_long_hidden_and_special_filenames(window, tmp_path):
    parent = tmp_path
    for _ in range(5):
        parent /= "long_folder_name_" * 3
    records = [make_record(parent / 'file [1] "quoted".png') if os.name != "nt"
               else make_record(parent / "file [1] 'quoted'.png"),
               make_record(tmp_path / ".hidden.png"),
               make_record(tmp_path / "\u65e5\u672c \U0001f30a.png")]
    window.start_scan([r.path for r in records])
    wait_until(lambda: not window.busy)
    assert window.model.rowCount() == 3
    assert all(row.modified_ns is not None for row in window.model.items)


def test_case_distinct_paths_remain_distinct_on_case_sensitive_fs(tmp_path):
    first = make_record(tmp_path / "Case.png")
    second = make_record(tmp_path / "case.png")
    if first.identity_key == second.identity_key:
        pytest.skip("Host filesystem paths are case-insensitive")
    plan = build_plan([first, second], PlanRequest("images", str(tmp_path / "out"), "jpg"))
    assert len({i.output for i in plan.items}) == 2


def test_empty_folder_is_a_successful_empty_scan(window, tmp_path):
    window.start_scan([str(tmp_path)])
    wait_until(lambda: not window.busy)
    assert window.model.rowCount() == 0
    assert not window.preview_button.isEnabled()


def test_close_cancels_preview_without_dangling_thread(window, monkeypatch):
    import media_tool.services as services
    def wait_plan(records, request, cancelled):
        import time
        while not cancelled():
            time.sleep(0.005)
        raise PlanCancelled()
    monkeypatch.setattr(services, "build_plan", wait_plan)
    window.model.append([FileRecord("sample://1", "x.png", 1, "Image", True)])
    window._tool_changed("Rename")
    assert window.preview_changes()
    window.close()
    wait_until(lambda: not window.busy)
    assert not window.isVisible()

def test_metadata_table_has_visible_rows_and_scroll_settings_match_theme(window):
    from PySide6.QtTest import QTest
    window.start_scan(sample_count=3)
    wait_until(lambda: not window.busy)
    for name, expected in (("dark", "#1d232d"), ("light", "#e8ede6")):
        window.set_theme(name)
        QTest.qWait(30)
        assert window.table.viewport().height() >= window.table.verticalHeader().defaultSectionSize() * 2
        color = window.settings_panel.grab().toImage().pixelColor(15, 15)
        assert color.name() == expected

def test_rename_destination_shows_same_folder_and_conversion_choice_is_retained(window, tmp_path):
    destination = str(tmp_path / "output")
    window.destination.setText(destination)
    window._tool_changed("Rename")
    assert window.destination.text() == ""
    assert not window.destination.isEnabled()
    window._tool_changed("Images")
    assert window.destination.text() == destination

def test_undecodable_filename_has_actionable_validation():
    assert "undecodable" in validate_name("bad" + chr(0xDCFF) + ".png")

def test_repeated_theme_changes_update_entire_workspace(window):
    from PySide6.QtWidgets import QWidget
    from PySide6.QtTest import QTest
    for theme, background, panel in (("light", "#dce3dd", "#e8ede6"),
                                    ("dark", "#15191f", "#1d232d"),
                                    ("light", "#dce3dd", "#e8ede6"),
                                    ("dark", "#15191f", "#1d232d")):
        window.set_theme(theme)
        QTest.qWait(20)
        workspace = window.findChild(QWidget, "workspace")
        assert workspace.grab().toImage().pixelColor(5, 5).name() == background
        assert window.file_layout.parentWidget().grab().toImage().pixelColor(5, 5).name() == panel
        assert window.settings_panel.grab().toImage().pixelColor(5, 5).name() == panel

def test_many_duplicate_basenames_get_stable_unique_outputs(tmp_path):
    records = [make_record(tmp_path / str(i) / "same.png") for i in range(40)]
    request = PlanRequest("images", str(tmp_path / "out"), "jpg")
    first = build_plan(records, request)
    second = build_plan(reversed(records), request)
    assert first == second
    assert len({item.output for item in first.items}) == 40
    assert all(item.status == "Planned" for item in first.items)
