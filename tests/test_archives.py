import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import zipfile

import pytest
from PySide6.QtCore import QItemSelectionModel, Qt

from media_tool.domain import FileRecord
from media_tool.jobs import Store, execute_job
from media_tool.planning import PlanRequest, PlanCancelled
from media_tool.zip_plan import plan_archives, archive_snapshot
from test_foundation import app, window, wait_until


def fixture_tree(tmp_path):
    source = tmp_path / "Pages 日本"
    (source / "nested").mkdir(parents=True)
    (source / "empty").mkdir()
    (source / "page 2.txt").write_bytes(b"second page\n" * 200)
    (source / "nested" / "page 10.txt").write_bytes(b"tenth page\n" * 400)
    (source / ".hidden").write_text("hidden content")
    (source / "skip.tmp").write_text("deliberately excluded")
    return source


def plan(paths, **options):
    return plan_archives([FileRecord.from_path(p) for p in paths], PlanRequest("archives", **options))


def run(tmp_path, preview, **options):
    store = Store(tmp_path / "history")
    try:
        job = store.enqueue("ZIP", "archives", archive_snapshot(preview))
        execute_job(store, job, **options)
        return store.items(job)
    finally:
        store.close()


@pytest.mark.parametrize("level", [0, 1, 6, 9])
def test_complete_folder_zip_reads_back_contents_structure_and_originals(tmp_path, level):
    source = fixture_tree(tmp_path)
    originals = {p: p.read_bytes() for p in source.rglob("*") if p.is_file()}
    preview = plan([source], archive_name="Complete 日本.zip", archive_level=level)
    assert preview.counts == {"Planned": 1}, preview.items
    output = Path(preview.items[0].output)
    assert not output.exists()
    item = run(tmp_path, preview)[0]
    assert item["state"] == "succeeded", item
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        assert archive.namelist() == ["Pages 日本/", "Pages 日本/.hidden", "Pages 日本/empty/", "Pages 日本/nested/", "Pages 日本/nested/page 10.txt", "Pages 日本/page 2.txt", "Pages 日本/skip.tmp"]
        for path, content in originals.items():
            assert archive.read("Pages 日本/" + path.relative_to(source).as_posix()) == content
            assert path.read_bytes() == content
        method = zipfile.ZIP_STORED if level == 0 else zipfile.ZIP_DEFLATED
        assert archive.getinfo("Pages 日本/page 2.txt").compress_type == method
    assert not list(tmp_path.glob("*.part"))


def test_combined_roots_resolve_duplicate_basenames_and_overlap(tmp_path):
    folder = fixture_tree(tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "same.txt").write_text("first")
    (second / "same.txt").write_text("second")
    (first / folder.name).write_text("file sharing a folder name")
    preview = plan([first / "same.txt", second / "same.txt", folder, folder / "page 2.txt", first / folder.name])
    assert preview.counts == {"Planned": 1}, preview.items
    item = run(tmp_path, preview)[0]
    assert item["state"] == "succeeded", item
    with zipfile.ZipFile(preview.items[0].output) as archive:
        assert archive.read("same.txt") == b"first"
        assert archive.read("same (2).txt") == b"second"
        assert sum(name.endswith("page 2.txt") for name in archive.namelist()) == 1
        assert len({name.rstrip("/").casefold() for name in archive.namelist()}) == len(archive.namelist())


def test_separate_zip_per_file_and_folder_collision_and_skip(tmp_path):
    folder = fixture_tree(tmp_path)
    file = tmp_path / "Pages 日本.txt"
    file.write_text("loose file")
    (tmp_path / "Pages 日本.zip").write_bytes(b"existing user output")
    preview = plan([folder, file], archive_mode="separate")
    assert preview.counts == {"Planned": 2}
    assert len({i.output for i in preview.items}) == 2
    assert [r["state"] for r in run(tmp_path, preview)] == ["succeeded", "succeeded"]
    assert (tmp_path / "Pages 日本.zip").read_bytes() == b"existing user output"
    skipped = plan([folder, file], archive_mode="separate", collision="skip")
    assert skipped.counts == {"Skipped": 2}


def test_exclusions_hidden_empty_folders_and_nested_destination(tmp_path):
    folder = fixture_tree(tmp_path)
    output = folder / "output"
    output.mkdir()
    (output / "existing.bin").write_bytes(b"not included")
    preview = plan([folder], destination=str(output), archive_hidden=False, archive_empty_dirs=False,
                   archive_exclusions="*.tmp; nested")
    assert preview.counts == {"Planned": 1}, preview.items
    assert len(preview.items[0].archive_spec["manifest"]["excluded"]) == 4
    assert run(tmp_path, preview)[0]["state"] == "succeeded"
    with zipfile.ZipFile(preview.items[0].output) as archive:
        assert archive.namelist() == ["Pages 日本/page 2.txt"]


@pytest.mark.parametrize("change", ["new_file", "removed_file", "replaced_file", "changed_bytes", "replaced_folder", "late_output", "corrupt_zip"])
def test_changed_sources_outputs_and_corrupt_zip_never_commit(tmp_path, change):
    folder = fixture_tree(tmp_path)
    preview = plan([folder])
    output = Path(preview.items[0].output)
    def mutate(point):
        if point != ("written" if change == "corrupt_zip" else "prepared"):
            return
        file = folder / "page 2.txt"
        if change == "new_file":
            (folder / "new.txt").write_text("new arrival")
        elif change == "removed_file":
            file.unlink()
        elif change == "replaced_file":
            file.rename(folder / "old.txt")
            file.write_text("replacement")
        elif change == "changed_bytes":
            info = file.stat()
            file.write_bytes(file.read_bytes().replace(b"second", b"Second"))
            os.utime(file, ns=(info.st_atime_ns, info.st_mtime_ns))
        elif change == "replaced_folder":
            (folder / "nested").rename(tmp_path / "old-nested")
            (folder / "nested").mkdir()
        elif change == "late_output":
            output.write_bytes(b"user output")
        elif change == "corrupt_zip":
            partial = next(tmp_path.glob("*.part"))
            partial.write_bytes(b"not a ZIP")
    item = run(tmp_path, preview, hook=mutate)[0]
    assert item["state"] == "failed", item
    assert output.exists() == (change == "late_output")
    if output.exists():
        assert output.read_bytes() == b"user output"
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize("point", ["created", "chunk", "verify_chunk", "prepared"])
def test_zip_cancel_retry_and_no_partial_final_output(tmp_path, point):
    folder = fixture_tree(tmp_path)
    preview = plan([folder])
    store = Store(tmp_path / "history")
    cancel = threading.Event()
    try:
        job = store.enqueue("ZIP", "archives", archive_snapshot(preview))
        assert execute_job(store, job, cancel.is_set, hook=lambda p: cancel.set() if p == point else None) == "cancelled"
        assert not Path(preview.items[0].output).exists() and not list(tmp_path.glob("*.part"))
        assert store.retry(job) == 1
        assert execute_job(store, job) == "succeeded", store.items(job)
    finally:
        store.close()


@pytest.mark.parametrize("point", ["created", "written", "prepared", "committed"])
def test_actual_process_loss_and_zip_recovery(tmp_path, point):
    source = fixture_tree(tmp_path)
    preview = plan([source])
    store = Store(tmp_path / "history")
    try:
        job = store.enqueue("ZIP", "archives", archive_snapshot(preview))
        code = """import os,sys
from media_tool.jobs import Store,execute_job
store=Store(sys.argv[1])
def crash(point):
    if point==sys.argv[3]: os._exit(73)
execute_job(store,sys.argv[2],hook=crash)
"""
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        result = subprocess.run([sys.executable, "-c", code, str(store.directory), job, point],
            env=env, timeout=20, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        assert result.returncode == 73, result.stderr.decode(errors="replace")
        assert store.recover() == 1 and store.retry(job) == 1
        assert execute_job(store, job) == "succeeded", store.items(job)
        with zipfile.ZipFile(preview.items[0].output) as archive:
            assert archive.testzip() is None
        if point == "committed":
            assert "Recovered" in store.items(job)[0]["message"]
    finally:
        store.close()


def test_old_timestamps_and_zip64_streaming_path(tmp_path, monkeypatch):
    file = tmp_path / "old.txt"
    file.write_bytes(b"ZIP64 fixture" * 1000)
    os.utime(file, (0, 0))
    # Exercise the real ZIP64 path with a small fixture; >4 GiB is a separate opt-in trial.
    monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 1000)
    preview = plan([file])
    item = run(tmp_path, preview)[0]
    assert item["state"] == "succeeded", item
    with zipfile.ZipFile(preview.items[0].output) as archive:
        info = archive.getinfo("old.txt")
        assert info.date_time == (1980, 1, 1, 0, 0, 0)
        assert info.extract_version >= 45 and archive.read(info) == file.read_bytes()


def test_ui_folder_intake_combined_direct_run_manifest_and_waiting_queue(window, tmp_path):
    source = fixture_tree(tmp_path)
    loose = tmp_path / "loose.txt"
    loose.write_text("loose input")
    window._tool_changed("Archives")
    window.format.setCurrentText("ZIP")
    assert window.start_rename_button.text() == "Create ZIP"
    assert not window.keep.isVisible() and window.plan_request().keep_originals
    assert not window.recursive.isVisible()
    window.start_scan([str(source), str(loose)])
    wait_until(lambda: not window.busy)
    assert window.model.rowCount() == 2 and any(r.kind == "Folder" for r in window.model.items)
    queue = window.ensure_jobs()
    waiting = queue.store.enqueue("Waiting", "sample", [dict(name="later", seconds=0)])
    window.preview_changes()
    wait_until(lambda: not window.busy)
    assert window.last_plan.counts == {"Planned": 1}, window.status.text()
    window.archive_contents_button.click()
    assert window.archive_contents_dialog.isVisible()
    window.archive_contents_dialog.close()
    window.start_rename_button.click()
    wait_until(lambda: not window.busy and not queue.enabled, timeout=30000)
    assert window.result_model.items[0]["state"] == "succeeded", window.status.text()
    assert window.model.rowCount() == 0 and not window.result_can_undo
    assert queue.store.items(waiting)[0]["attempts"] == 0
    assert source.exists() and loose.exists() and not queue.isVisible()


def test_ui_separate_mode_freezes_settings_and_incomplete_intake_blocks(window, tmp_path):
    source = fixture_tree(tmp_path)
    window._tool_changed("Archives")
    window.format.setCurrentText("ZIP")
    window.archive_controls.mode.setCurrentIndex(window.archive_controls.mode.findData("separate"))
    assert not window.archive_controls.name.isVisible()
    window.start_scan([str(source), str(tmp_path / "missing")])
    wait_until(lambda: not window.busy)
    assert not window.preview_changes() and "incomplete" in window.status.text()
    window.clear_files()
    window.start_scan([str(source)])
    wait_until(lambda: not window.busy)
    window.preview_changes()
    wait_until(lambda: not window.busy)
    window.run_button.click()
    queue = window.jobs_dialog
    job = queue.store.jobs()[-1]
    spec = json.loads(queue.store.items(job["id"])[0]["spec"])
    window.archive_controls.level.setCurrentIndex(0)
    assert spec["level"] == 6 and not window.last_plan
    assert queue.store.items(job["id"])[0]["attempts"] == 0


@pytest.mark.parametrize("mode", ["combined", "separate"])
@pytest.mark.parametrize("select_rows", [True, False])
def test_ui_archive_mode_respects_selected_or_visible_inputs(window, tmp_path, mode, select_rows):
    sources = tmp_path / "inputs"
    output = tmp_path / "output"
    sources.mkdir()
    output.mkdir()
    contents = {name: name.encode() for name in ("file1.txt", "file2.txt", "file10.txt", "other.txt")}
    for name, data in contents.items():
        (sources / name).write_bytes(data)
    window._tool_changed("Archives")
    window.format.setCurrentText("ZIP")
    window.archive_controls.mode.setCurrentIndex(window.archive_controls.mode.findData(mode))
    # Pasted destinations and proxy sorting/filtering must work with either mode.
    assert not window.destination.isReadOnly()
    window.destination.setText(str(output))
    window.start_scan([str(sources / name) for name in contents])
    wait_until(lambda: not window.busy)
    window.name_filter.setText("file")
    window.apply_filters()
    window.table.sortByColumn(0, Qt.SortOrder.DescendingOrder)
    expected = {"file2.txt", "file10.txt"} if select_rows else set(contents) - {"other.txt"}
    if select_rows:
        selection = window.table.selectionModel()
        for row in range(window.proxy.rowCount()):
            index = window.proxy.index(row, 0)
            record = window.model.items[window.proxy.mapToSource(index).row()]
            if record.name in expected:
                selection.select(index, QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        assert len(selection.selectedRows()) == 2
    assert window.preview_changes()
    wait_until(lambda: not window.busy)
    count = 1 if mode == "combined" else len(expected)
    assert window.last_plan.counts == {"Planned": count}, window.status.text()
    planned_sources = {Path(entry["source"]).name for item in window.last_plan.items
                       for entry in item.archive_spec["inputs"]}
    assert planned_sources == expected
    window.start_rename_button.click()
    queue = window.jobs_dialog
    wait_until(lambda: not window.busy and not queue.enabled, timeout=30000)
    assert window.result_model.rowCount() == count
    assert all(item["state"] == "succeeded" for item in window.result_model.items), window.status.text()
    archives = list(output.glob("*.zip"))
    assert len(archives) == count
    actual = {}
    for path in archives:
        with zipfile.ZipFile(path) as archive:
            assert archive.testzip() is None
            if mode == "separate":
                assert len(archive.namelist()) == 1
            actual.update({name: archive.read(name) for name in archive.namelist()})
    assert actual == {name: contents[name] for name in expected}
    assert {record.name for record in window.model.items} == set(contents) - expected
    assert all((sources / name).read_bytes() == data for name, data in contents.items())
