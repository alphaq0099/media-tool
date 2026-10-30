import json
import os
from pathlib import Path
import threading

import pytest

from media_tool.domain import FileRecord
from media_tool.images import plan_images
from media_tool.jobs import SCHEMA, Store, execute_job
from media_tool.planning import PlanRequest
from test_foundation import app, window, wait_until
from test_video import media_tools, clip, run_plan
from test_video_formats import plan


@pytest.fixture
def fake_trash(tmp_path, monkeypatch):
    import media_tool.original_cleanup as cleanup
    folder = tmp_path / "isolated-trash"
    folder.mkdir()
    def move(path):
        target = folder / path.name
        path.rename(target)
        return str(target)
    monkeypatch.setattr(cleanup, "move_to_trash", move)
    return folder


def test_video_original_moves_only_after_verified_commit(tmp_path, media_tools, fake_trash):
    source = clip(tmp_path / "source.mp4")
    before = Path(source.path).read_bytes()
    preview = plan([source], "mkv", "h264", video_action="remux", keep_originals=False)
    assert "Recycle Bin" in preview.items[0].reason
    item = run_plan(tmp_path, preview)[0]
    assert item["state"] == "succeeded", dict(item)
    assert Path(preview.items[0].output).exists()
    assert not Path(source.path).exists()
    assert (fake_trash / source.name).read_bytes() == before
    assert json.loads(item["original_cleanup"])["state"] == "moved"


def test_image_original_moves_after_verified_commit(tmp_path, fake_trash):
    from PIL import Image
    from media_tool.images import image_snapshot
    source = tmp_path / "image.png"
    Image.new("RGB", (40, 30), "red").save(source)
    preview = plan_images([FileRecord.from_path(source)], PlanRequest("images", extension="jpg", keep_originals=False))
    store = Store(tmp_path / "history")
    try:
        job = store.enqueue("Images", "images", image_snapshot(preview))
        assert execute_job(store, job) == "succeeded", store.items(job)
        assert not source.exists() and (fake_trash / source.name).exists()
        assert Path(preview.items[0].output).exists()
    finally:
        store.close()


def test_failed_trash_keeps_verified_output_successful(tmp_path, media_tools, monkeypatch):
    import media_tool.original_cleanup as cleanup
    monkeypatch.setattr(cleanup, "move_to_trash", lambda path: (_ for _ in ()).throw(OSError("Trash unavailable")))
    source = clip(tmp_path / "source.mp4")
    preview = plan([source], "mkv", "h264", video_action="remux", keep_originals=False)
    item = run_plan(tmp_path, preview)[0]
    assert item["state"] == "succeeded", dict(item)
    assert Path(source.path).exists() and Path(preview.items[0].output).exists()
    assert "cleanup skipped" in item["message"]


def test_audio_selector_and_keep_originals_are_editable(window):
    window._tool_changed("Video")
    assert [window.video_audio_mode.itemData(n) for n in range(window.video_audio_mode.count())] == ["all", "custom", "none"]
    window.video_audio_mode.setCurrentIndex(window.video_audio_mode.findData("custom"))
    window.video_audio_tracks.setText("1")
    assert window.plan_request().video_audio_tracks == "1"
    assert window.keep.isChecked() and window.keep.isEnabled()
    window.keep.setChecked(False)
    assert not window.plan_request().keep_originals
    assert window.keep.toolTip()  # Explanation moved to a tooltip in the revamp.


def image_job(tmp_path, store):
    from PIL import Image
    from media_tool.images import image_snapshot
    source = tmp_path / "original.png"
    Image.new("RGB", (40, 30), "blue").save(source)
    preview = plan_images([FileRecord.from_path(source)], PlanRequest("images", extension="jpg", keep_originals=False))
    specs = image_snapshot(preview)
    return source, Path(preview.items[0].output), store.enqueue("Images", "images", specs), specs


@pytest.mark.parametrize("point", ["created", "written", "prepared"])
def test_cancel_before_commit_keeps_original(tmp_path, fake_trash, point):
    store = Store(tmp_path / "history")
    cancelled = threading.Event()
    try:
        source, output, job, _ = image_job(tmp_path, store)
        before = source.read_bytes()
        assert execute_job(store, job, cancelled.is_set, hook=lambda p: cancelled.set() if p == point else None) == "cancelled"
        assert source.read_bytes() == before and not output.exists()
        assert not list(fake_trash.iterdir())
    finally:
        store.close()


@pytest.mark.parametrize("change", ["source", "output", "cancel", "pending_job", "failed_conversion"])
def test_cleanup_boundary_keeps_original_and_reports_reason(tmp_path, fake_trash, change):
    store = Store(tmp_path / "history")
    cancelled = threading.Event()
    try:
        source, output, job, specs = image_job(tmp_path, store)
        if change == "pending_job":
            store.enqueue("Needs same input", "images", specs)
        def boundary(point):
            if change == "failed_conversion" and point == "created":
                raise OSError("Encoder failed")
            if point != "original_cleanup_prepared":
                return
            if change == "cancel":
                cancelled.set()
            elif change in ("source", "output"):
                path = source if change == "source" else output
                info = path.stat()
                contents = bytearray(path.read_bytes())
                contents[-1] ^= 1
                path.write_bytes(contents)
                os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        result = execute_job(store, job, cancelled.is_set, hook=boundary)
        assert source.exists() and not list(fake_trash.iterdir())
        if change == "failed_conversion":
            assert result == "failed" and not output.exists()
        else:
            assert result == "succeeded" and output.exists()
            item = store.items(job)[0]
            assert json.loads(item["original_cleanup"])["state"] == "kept", dict(item)
            assert "Original" in item["message"] or "original" in item["message"]
    finally:
        store.close()


@pytest.mark.parametrize("replacement", [False, True])
def test_interruption_after_trash_recovers_output_and_keeps_replacement(tmp_path, fake_trash, replacement):
    store = Store(tmp_path / "history")
    try:
        source, output, job, _ = image_job(tmp_path, store)
        before = source.read_bytes()
        def crash(point):
            if point == "original_trashed":
                raise SystemExit("Simulated process loss before cleanup journal commit")
        with pytest.raises(SystemExit):
            execute_job(store, job, hook=crash)
        assert output.exists() and not source.exists()
        assert json.loads(store.items(job)[0]["original_cleanup"])["state"] == "pending"
        store.close()
        store = Store(tmp_path / "history")
        if replacement:
            source.write_bytes(b"new user file at the old name")
        assert store.recover() == 1 and store.retry(job) == 1
        assert execute_job(store, job) == "succeeded", store.items(job)
        assert (fake_trash / source.name).read_bytes() == before
        assert source.exists() == replacement
        if replacement:
            assert source.read_bytes() == b"new user file at the old name"
    finally:
        store.close()


def test_schema13_upgrade_keeps_legacy_originals(tmp_path, fake_trash):
    store = Store(tmp_path / "history")
    source, output, job, _ = image_job(tmp_path, store)
    item = store.items(job)[0]
    spec = json.loads(item["spec"])
    del spec["keep_originals"]
    store.db.execute("UPDATE items SET spec=? WHERE id=?", (json.dumps(spec), item["id"]))
    store.db.execute("ALTER TABLE items DROP COLUMN source_digest")
    store.db.execute("ALTER TABLE items DROP COLUMN original_cleanup")
    store.db.execute("PRAGMA user_version=13")
    store.db.commit()
    store.close()
    store = Store(tmp_path / "history")
    try:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA
        assert execute_job(store, job) == "succeeded", store.items(job)
        assert source.exists() and output.exists() and not list(fake_trash.iterdir())
        assert store.items(job)[0]["original_cleanup"] is None
    finally:
        store.close()
