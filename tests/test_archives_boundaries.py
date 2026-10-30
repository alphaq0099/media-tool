import errno
import json
import os
from pathlib import Path
import shutil
import threading
import zipfile

import pytest

from media_tool.domain import FileRecord
from media_tool.jobs import SCHEMA, Store, execute_job, copy_snapshot
from media_tool.planning import PlanCancelled
from media_tool.zip_plan import plan_archives, safe_name, archive_snapshot
from test_archives import fixture_tree, plan, run


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/drive", "root/../escape", "root\\escape", "root/CON.txt", "root/a:stream", "root/a.", "a//b", "./a"])
def test_unsafe_entry_names_are_blocked(name):
    with pytest.raises(ValueError):
        safe_name(name)


@pytest.mark.parametrize("options", [dict(archive_name="../bad"), dict(archive_name=""), dict(archive_name="NUL"),
    dict(archive_mode="unsupported"), dict(archive_level=4), dict(archive_exclusions="../secret"), dict(archive_exclusions="/absolute/*"),
    dict(destination="relative-folder")])
def test_invalid_requests_never_create_archives(tmp_path, options):
    source = fixture_tree(tmp_path)
    with pytest.raises((ValueError, OSError)):
        plan([source], **options)
    assert not list(tmp_path.glob("*.zip"))


def test_folder_permission_failure_blocks_complete_manifest(tmp_path, monkeypatch):
    import media_tool.zip_plan as engine
    source = fixture_tree(tmp_path)
    real = engine.os.scandir
    def denied(path):
        if Path(path).name == "nested":
            raise PermissionError("unreadable folder")
        return real(path)
    monkeypatch.setattr(engine.os, "scandir", denied)
    preview = plan([source])
    assert preview.counts == {"Invalid": 1}
    assert "unreadable folder" in preview.items[0].reason
    with pytest.raises(ValueError):
        archive_snapshot(preview)


def test_relative_path_exclusion_matches_inside_selected_root(tmp_path):
    source = fixture_tree(tmp_path)
    preview = plan([source], archive_exclusions="nested/*.txt")
    assert run(tmp_path, preview)[0]["state"] == "succeeded"
    with zipfile.ZipFile(preview.items[0].output) as archive:
        assert "Pages 日本/nested/page 10.txt" not in archive.namelist()
        assert "Pages 日本/page 2.txt" in archive.namelist()


def test_explicit_exclusion_can_omit_unreadable_metadata(tmp_path, monkeypatch):
    source = fixture_tree(tmp_path)
    real = Path.lstat
    def inaccessible(path, *args, **kwargs):
        if path == source / "skip.tmp":
            raise PermissionError("cannot inspect excluded file")
        return real(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", inaccessible)
    preview = plan([source], archive_exclusions="*.tmp")
    assert preview.counts == {"Planned": 1}, preview.items
    assert run(tmp_path, preview)[0]["state"] == "succeeded"


def test_read_denied_after_preview_keeps_all_originals(tmp_path, monkeypatch):
    folder = fixture_tree(tmp_path)
    preview = plan([folder])
    real = Path.open
    def denied(path, *args, **kwargs):
        if path == folder / "page 2.txt":
            raise PermissionError("source read denied")
        return real(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", denied)
    item = run(tmp_path, preview)[0]
    assert item["state"] == "failed" and "read denied" in item["message"]
    assert not Path(preview.items[0].output).exists() and not list(tmp_path.glob("*.part"))


def test_link_in_folder_blocks_unless_explicitly_excluded(tmp_path):
    source = fixture_tree(tmp_path)
    target = tmp_path / "outside.txt"
    target.write_text("outside selected folder")
    link = source / "external-link"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Windows symlink privilege unavailable")
    assert plan([source]).counts == {"Invalid": 1}
    preview = plan([source], archive_exclusions="external-link")
    assert run(tmp_path, preview)[0]["state"] == "succeeded"
    with zipfile.ZipFile(preview.items[0].output) as archive:
        assert all("external-link" not in name for name in archive.namelist())
    assert target.read_text() == "outside selected folder"


def test_discovery_cancel_and_manifest_limit_are_explicit(tmp_path, monkeypatch):
    import media_tool.zip_plan as engine
    source = fixture_tree(tmp_path)
    with pytest.raises(PlanCancelled):
        plan_archives([FileRecord.from_path(source)], plan([source]).request, cancelled=lambda: True)
    monkeypatch.setattr(engine, "MAX_ENTRIES", 3)
    preview = plan([source])
    assert preview.counts == {"Invalid": 1} and "exceeds" in preview.items[0].reason


def test_partial_disk_full_failure_can_retry(tmp_path, monkeypatch):
    import media_tool.zip_archive as engine
    source = fixture_tree(tmp_path)
    preview = plan([source])
    real = engine.write_zip
    def full(spec, destination, *args):
        destination.write(b"partial")
        raise OSError(errno.ENOSPC, "No space left")
    monkeypatch.setattr(engine, "write_zip", full)
    store = Store(tmp_path / "history")
    try:
        job = store.enqueue("ZIP", "archives", archive_snapshot(preview))
        assert execute_job(store, job) == "failed"
        assert not Path(preview.items[0].output).exists() and not list(tmp_path.glob("*.part"))
        monkeypatch.setattr(engine, "write_zip", real)
        assert store.retry(job) == 1 and execute_job(store, job) == "succeeded", store.items(job)
    finally:
        store.close()


def test_foreign_partial_replacement_is_kept(tmp_path):
    source = fixture_tree(tmp_path)
    preview = plan([source])
    replaced = []
    def replace(point):
        if point == "written":
            partial = next(tmp_path.glob("*.part"))
            partial.rename(tmp_path / "saved-owned-part")
            partial.write_bytes(b"user replacement")
            replaced.append(partial)
    item = run(tmp_path, preview, hook=replace)[0]
    assert item["state"] == "failed" and not Path(preview.items[0].output).exists()
    assert replaced[0].read_bytes() == b"user replacement"


def test_store_mode_insufficient_space_blocks_before_creating_partial(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import media_tool.zip_archive as engine
    source = fixture_tree(tmp_path)
    preview = plan([source], archive_level=0)
    monkeypatch.setattr(engine.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
    item = run(tmp_path, preview)[0]
    assert item["state"] == "failed" and "free space" in item["message"]
    assert not list(tmp_path.glob("*.part")) and not Path(preview.items[0].output).exists()


def test_changed_committed_zip_is_not_claimed_on_recovery(tmp_path):
    source = fixture_tree(tmp_path)
    preview = plan([source])
    store = Store(tmp_path / "history")
    try:
        job = store.enqueue("ZIP", "archives", archive_snapshot(preview))
        def crash(point):
            if point == "committed":
                raise SystemExit()
        with pytest.raises(SystemExit):
            execute_job(store, job, hook=crash)
        output = Path(preview.items[0].output)
        info = output.stat()
        content = bytearray(output.read_bytes())
        content[30] ^= 1
        output.write_bytes(content)
        os.utime(output, ns=(info.st_atime_ns, info.st_mtime_ns))
        assert store.recover() == 1 and store.retry(job) == 1
        assert execute_job(store, job) == "failed" and "committed ZIP changed" in store.items(job)[0]["message"]
        assert output.read_bytes() == content
    finally:
        store.close()


def test_combined_zip_inside_selected_parent_omits_own_output(tmp_path):
    parent = fixture_tree(tmp_path)
    nested = parent / "nested"
    preview = plan([parent, nested], archive_mode="separate")
    assert preview.counts == {"Planned": 2}
    result = run(tmp_path, preview)
    assert all(item["state"] == "succeeded" for item in result), result
    for item in preview.items:
        with zipfile.ZipFile(item.output) as archive:
            assert not any(name.endswith(".zip") for name in archive.namelist())


def test_schema14_saved_copy_runs_after_archive_upgrade(tmp_path):
    source = tmp_path / "before.txt"
    source.write_text("legacy copy")
    output = tmp_path / "copies"
    output.mkdir()
    store = Store(tmp_path / "history")
    job = store.enqueue("Old copy", "copy", copy_snapshot([FileRecord.from_path(source)], str(output)))
    store.db.execute("PRAGMA user_version=14")
    store.close()
    store = Store(tmp_path / "history")
    try:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA
        assert execute_job(store, job) == "succeeded"
        assert (output / source.name).read_bytes() == source.read_bytes()
    finally:
        store.close()


@pytest.mark.parametrize("folder_input", [False, True])
def test_media_cleanup_keeps_sources_needed_by_queued_zip(tmp_path, monkeypatch, folder_input):
    from PIL import Image
    from media_tool.images import plan_images, image_snapshot
    from media_tool.planning import PlanRequest
    import media_tool.original_cleanup as cleanup
    folder = tmp_path / "source"
    folder.mkdir()
    source = folder / "z.png"
    Image.new("RGB", (20, 10), "red").save(source)
    other = folder / "a.txt"
    other.write_text("first root")
    archive = plan([folder] if folder_input else [other, source])
    image = plan_images([FileRecord.from_path(source)], PlanRequest("images", extension="jpg", keep_originals=False))
    monkeypatch.setattr(cleanup, "move_to_trash", lambda path: pytest.fail("Queued ZIP still needs this original"))
    store = Store(tmp_path / "history")
    try:
        waiting = store.enqueue("ZIP", "archives", archive_snapshot(archive))
        job = store.enqueue("Convert", "images", image_snapshot(image))
        assert execute_job(store, job) == "succeeded", store.items(job)
        assert source.exists() and "pending job" in store.items(job)[0]["message"]
        # The conversion output is a new member of folder input, so its ZIP preview
        # must be refreshed; selected-file ZIPs remain valid without tree changes.
        if not folder_input:
            assert execute_job(store, waiting) == "succeeded", store.items(waiting)
    finally:
        store.close()


@pytest.mark.skipif(os.environ.get("MEDIATOOL_LARGE_ZIP") != "1", reason="Opt-in >4 GiB sparse-file ZIP64 trial")
def test_real_file_over_four_gib_uses_zip64_with_bounded_memory(tmp_path, record_testsuite_property):
    size = 4 * 1024**3 + 37
    # Windows truncate can allocate/zero the extended region even on a sparse
    # handle. Require capacity for the entire fixture before extending it.
    if shutil.disk_usage(tmp_path).free < size + 512 * 1024**2:
        pytest.skip("Not enough free space for >4 GiB source plus reserve")
    source = tmp_path / "large sparse.bin"
    with source.open("wb") as handle:
        handle.truncate(size)
        handle.seek(0)
        handle.write(b"large sparse ZIP")
        handle.seek(size - 3)
        handle.write(b"end")
    preview = plan([source], archive_level=1)
    item = run(tmp_path, preview)[0]
    assert item["state"] == "succeeded", item
    with zipfile.ZipFile(preview.items[0].output) as archive:
        info = archive.getinfo(source.name)
        assert info.file_size == size and info.extract_version >= 45
        with archive.open(info) as stream:
            assert stream.read(16) == b"large sparse ZIP"
            stream.seek(size - 3)
            assert stream.read() == b"end"
    record_testsuite_property("source_logical_bytes", size)
    record_testsuite_property("zip_bytes", Path(preview.items[0].output).stat().st_size)
