import hashlib
import json
import os
from pathlib import Path
import threading
import subprocess
import sys
import zlib

import pytest
from PIL import Image, ImageCms

from media_tool.domain import FileRecord
from media_tool.images import plan_images, image_snapshot
from media_tool.jobs import SCHEMA, Store, execute_job
from media_tool.planning import PlanRequest
from test_foundation import app, window, wait_until


def fixture_image(path, mode="RGB", color=(30, 90, 180), size=(24, 16), **options):
    path.parent.mkdir(parents=True, exist_ok=True)
    with Image.new(mode, size, color) as image:
        image.save(path, **options)
    return FileRecord.from_path(path)


def plan(tmp_path, records, extension="png", **options):
    destination = tmp_path / "out"
    destination.mkdir(exist_ok=True)
    return plan_images(records, PlanRequest("images", str(destination), extension, **options))


def run(tmp_path, preview, **options):
    with_store = Store(tmp_path / "history")
    try:
        job = with_store.enqueue("Images", "images", image_snapshot(preview))
        execute_job(with_store, job, **options)
        return with_store.items(job)
    finally:
        with_store.close()


@pytest.mark.parametrize("source_ext", ["jpg", "png", "webp", "bmp", "tiff"])
@pytest.mark.parametrize("extension", ["jpg", "png", "webp"])
def test_conversion_matrix_keeps_original_and_decodes(tmp_path, source_ext, extension):
    source = fixture_image(tmp_path / f"original.{source_ext}")
    original = Path(source.path).read_bytes()
    preview = plan(tmp_path, [source], extension)
    assert preview.counts == {"Planned": 1}
    items = run(tmp_path, preview)
    assert items[0]["state"] == "succeeded", items
    with Image.open(preview.items[0].output) as result:
        result.load()
        assert result.format == {"jpg": "JPEG", "png": "PNG", "webp": "WEBP"}[extension]
        assert result.size == (24, 16)
    assert Path(source.path).read_bytes() == original
    assert Path(source.path).stat().st_mtime_ns == source.modified_ns
    assert not list((tmp_path / "out").glob("*.part"))


@pytest.mark.parametrize("background,pixel", [("#ffffff", (255, 255, 255)), ("#000000", (0, 0, 0))])
def test_transparent_jpeg_explicit_background(tmp_path, background, pixel):
    source = fixture_image(tmp_path / "alpha.png", "RGBA", (255, 0, 0, 0))
    preview = plan(tmp_path, [source], "jpg", alpha_background=background)
    assert run(tmp_path, preview)[0]["state"] == "succeeded"
    with Image.open(preview.items[0].output) as output:
        assert output.getpixel((10, 10)) == pixel


@pytest.mark.parametrize("extension", ["png", "webp"])
def test_alpha_preserved_in_supporting_outputs(tmp_path, extension):
    source = fixture_image(tmp_path / "alpha.png", "RGBA", (255, 0, 0, 110))
    preview = plan(tmp_path, [source], extension)
    assert run(tmp_path, preview)[0]["state"] == "succeeded"
    with Image.open(preview.items[0].output) as output:
        assert output.convert("RGBA").getpixel((10, 10))[3] == 110


@pytest.mark.parametrize("metadata", ["remove", "preserve"])
@pytest.mark.parametrize("extension", ["jpg", "png", "webp"])
def test_orientation_icc_and_exif_policy(tmp_path, metadata, extension):
    exif = Image.Exif()
    exif[274], exif[315] = 6, "Fixture photographer"
    srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    source = fixture_image(tmp_path / "rotated.jpg", exif=exif, icc_profile=srgb, dpi=(96, 96))
    preview = plan(tmp_path, [source], extension, metadata=metadata)
    assert preview.items[0].image_spec["image"]["output_size"] == [16, 24]
    items = run(tmp_path, preview)
    assert items[0]["state"] == "succeeded", items
    with Image.open(preview.items[0].output) as output:
        assert output.size == (16, 24)
        assert output.getexif().get(274, 1) == 1
        assert bool(output.info.get("icc_profile")) == (metadata == "preserve")
        assert output.getexif().get(315) == ("Fixture photographer" if metadata == "preserve" else None)
        assert max(abs(a-b) for a,b in zip(output.convert("RGB").getpixel((2, 2)), (30,90,180))) < 8


def test_unprofiled_cmyk_explicit_policy(tmp_path):
    source = fixture_image(tmp_path / "print.jpg", "CMYK", (0, 255, 255, 0))
    preview = plan(tmp_path, [source])
    assert "approximate" in preview.items[0].reason
    assert run(tmp_path, preview)[0]["state"] == "succeeded"
    with Image.open(preview.items[0].output) as output:
        assert output.getpixel((2, 2)) == (255, 0, 0)


@pytest.mark.parametrize("kind", ["corrupt", "gif", "animated", "multipage", "16bit", "huge"])
def test_unsupported_inputs_block_without_outputs(tmp_path, kind, monkeypatch):
    source = tmp_path / "bad.png"
    if kind == "corrupt":
        source.write_bytes(b"not a PNG")
    elif kind == "gif":
        Image.new("RGB", (3, 3)).save(source, format="GIF")
    elif kind in ("animated", "multipage"):
        Image.new("RGB", (3, 3), "red").save(source, format="PNG" if kind == "animated" else "TIFF",
            save_all=True, append_images=[Image.new("RGB", (3, 3), "blue")])
    elif kind == "16bit":
        Image.new("I;16", (3, 3), 256).save(source, format="TIFF")
    else:
        fixture_image(source)
        data = bytearray(source.read_bytes())
        data[16:24] = (40001).to_bytes(4, "big") + (1000).to_bytes(4, "big")
        data[29:33] = zlib.crc32(data[12:29]).to_bytes(4, "big")
        source.write_bytes(data)
    before = source.read_bytes()
    preview = plan(tmp_path, [FileRecord.from_path(source)])
    assert preview.counts == {"Invalid": 1}
    if kind == "huge":
        assert "40 megapixel" in preview.items[0].reason
    with pytest.raises(ValueError, match="invalid or unsupported"):
        image_snapshot(preview)
    assert not list((tmp_path / "out").iterdir())
    assert source.read_bytes() == before


def test_same_basename_formats_and_existing_outputs(tmp_path):
    sources = [fixture_image(tmp_path / "same.jpg"), fixture_image(tmp_path / "same.png")]
    preview = plan(tmp_path, sources, "webp")
    assert {Path(i.output).name for i in preview.items} == {"same.webp", "same_1.webp"}
    assert all(i["state"] == "succeeded" for i in run(tmp_path, preview))
    skipped = plan(tmp_path, sources, "webp", collision="skip")
    assert skipped.counts == {"Skipped": 2}


@pytest.mark.parametrize("point", ["created", "written", "prepared"])
def test_cancel_removes_only_owned_partial_and_retry_works(tmp_path, point):
    source = fixture_image(tmp_path / "original.png")
    original = Path(source.path).read_bytes()
    preview = plan(tmp_path, [source], "jpg")
    cancelled = threading.Event()
    store = Store(tmp_path / "history")
    try:
        job = store.enqueue("Images", "images", image_snapshot(preview))
        execute_job(store, job, cancelled.is_set, hook=lambda event: cancelled.set() if event == point else None)
        assert store.items(job)[0]["state"] == "cancelled"
        assert not list((tmp_path / "out").iterdir())
        store.retry(job)
        execute_job(store, job)
        assert store.items(job)[0]["state"] == "succeeded"
        assert store.items(job)[0]["attempts"] == 2
        assert Path(source.path).read_bytes() == original
    finally:
        store.close()


def test_output_race_never_overwrites(tmp_path):
    source = fixture_image(tmp_path / "original.png")
    preview = plan(tmp_path, [source], "jpg")
    output = Path(preview.items[0].output)
    items = run(tmp_path, preview, hook=lambda event: output.write_bytes(b"someone else's file") if event == "prepared" else None)
    assert items[0]["state"] == "failed"
    assert output.read_bytes() == b"someone else's file"
    assert not list(output.parent.glob("*.part"))


def test_changed_source_blocks_commit_even_with_same_identity_size_and_time(tmp_path):
    source = fixture_image(tmp_path / "original.bmp")
    preview = plan(tmp_path, [source])
    def mutate(event):
        if event == "written":
            path = Path(source.path)
            data = bytearray(path.read_bytes())
            data[-1] ^= 1
            path.write_bytes(data)
            os.utime(path, ns=(source.modified_ns, source.modified_ns))
    items = run(tmp_path, preview, hook=mutate)
    assert items[0]["state"] == "failed"
    assert "Original changed" in items[0]["message"]
    assert not list((tmp_path / "out").iterdir())


def test_bad_icc_fails_without_output(tmp_path):
    source = fixture_image(tmp_path / "badprofile.png", icc_profile=b"invalid color profile")
    preview = plan(tmp_path, [source])
    assert run(tmp_path, preview)[0]["state"] == "failed"
    assert not list((tmp_path / "out").iterdir())


def test_recover_committed_output_after_interruption(tmp_path):
    source = fixture_image(tmp_path / "original.jpg")
    preview = plan(tmp_path, [source])
    store = Store(tmp_path / "history")
    job = store.enqueue("Images", "images", image_snapshot(preview))
    def crash(point):
        if point == "committed":
            raise SystemExit("simulated process loss")
    try:
        with pytest.raises(SystemExit):
            execute_job(store, job, hook=crash)
    finally:
        store.close()
    store = Store(tmp_path / "history")
    try:
        store.recover()
        store.retry(job)
        execute_job(store, job)
        assert store.items(job)[0]["state"] == "succeeded"
        assert "Recovered" in store.items(job)[0]["message"]
        assert len(list((tmp_path / "out").iterdir())) == 1
    finally:
        store.close()


def test_ui_direct_conversion_and_comparison_leave_waiting_jobs_paused(window, tmp_path):
    source = fixture_image(tmp_path / "alpha.png", "RGBA", (255, 0, 0, 90))
    out = tmp_path / "out"
    out.mkdir()
    window.destination.setText(str(out))
    window.start_scan([source.path])
    wait_until(lambda: not window.busy)
    queue = window.ensure_jobs()
    waiting = queue.store.enqueue("Waiting", "sample", [{"name": "wait", "seconds": 0}])
    window.start_rename_button.click()
    wait_until(lambda: not window.busy and not queue.enabled)
    assert window.tabs.currentIndex() == 2, window.status.text()
    assert window.model.rowCount() == 0
    assert window.result_model.items[0]["state"] == "succeeded"
    assert not window.result_can_undo
    assert not queue.isVisible()
    assert queue.store.items(waiting)[0]["attempts"] == 0
    assert Path(source.path).exists()
    window.results_table.selectRow(0)
    window.compare_button.click()
    dialog = window.comparison_dialog
    wait_until(lambda: dialog.worker is None)
    assert len(dialog.previews) == 2, dialog.note.text()
    assert all(not preview.isNull() for preview, _ in dialog.previews)
    dialog.close()


def test_ui_invalid_batch_is_actionable_and_does_not_run(window, tmp_path):
    source = tmp_path / "bad.png"
    source.write_bytes(b"bad input")
    window.destination.setText(str(tmp_path))
    window.start_scan([str(source)])
    wait_until(lambda: not window.busy)
    window.start_rename_button.click()
    wait_until(lambda: not window.busy)
    assert "invalid or unsupported" in window.status.text()
    assert not window.jobs_dialog.store.jobs()
    assert window.plan_model.items[0].status == "Invalid"
    assert window.model.rowCount() == 1


@pytest.mark.parametrize("point", ["created", "written", "prepared", "committed"])
def test_real_process_loss_recovers_images(tmp_path, point):
    source = fixture_image(tmp_path / "original.jpg")
    preview = plan(tmp_path, [source])
    store = Store(tmp_path / "history")
    try:
        job = store.enqueue("Images", "images", image_snapshot(preview))
        code = """import os,sys
from media_tool.jobs import Store,execute_job
s=Store(sys.argv[1])
def crash(point):
    if point == sys.argv[3]:
        os._exit(73)
execute_job(s,sys.argv[2],hook=crash)
"""
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        result = subprocess.run([sys.executable, "-c", code, str(store.directory), job, point],
                                env=env, timeout=15, capture_output=True)
        assert result.returncode == 73, result.stderr.decode(errors="replace")
        assert store.recover() == 1
        assert store.retry(job) == 1
        assert execute_job(store, job) == "succeeded"
        assert len(list((tmp_path / "out").iterdir())) == 1
        with Image.open(preview.items[0].output) as output:
            output.load()
            assert output.size == (24, 16)
    finally:
        store.close()


def test_unrelated_replacement_of_partial_is_never_deleted(tmp_path):
    source = fixture_image(tmp_path / "original.jpg")
    preview = plan(tmp_path, [source])
    store = Store(tmp_path / "history")
    try:
        job = store.enqueue("Images", "images", image_snapshot(preview))
        def crash(point):
            if point == "written":
                raise SystemExit()
        with pytest.raises(SystemExit):
            execute_job(store, job, hook=crash)
        temp = Path(store.items(job)[0]["temp"])
        temp.rename(temp.parent / "moved-part")
        temp.write_bytes(b"unrelated")
        store.recover()
        store.retry(job)
        assert execute_job(store, job) == "failed"
        assert temp.read_bytes() == b"unrelated"
        assert not Path(preview.items[0].output).exists()
    finally:
        store.close()


def test_source_replaced_after_preview_blocks_conversion(tmp_path):
    source = fixture_image(tmp_path / "original.jpg")
    preview = plan(tmp_path, [source])
    Path(source.path).write_bytes(b"changed")
    assert run(tmp_path, preview)[0]["state"] == "failed"
    assert not list((tmp_path / "out").iterdir())


def test_decode_failure_does_not_commit_truncated_jpeg(tmp_path):
    source = fixture_image(tmp_path / "truncated.jpg", size=(80, 60))
    path = Path(source.path)
    path.write_bytes(path.read_bytes()[:-10])
    preview = plan(tmp_path, [FileRecord.from_path(path)])
    if preview.counts == {"Planned": 1}:
        assert run(tmp_path, preview)[0]["state"] == "failed"
    else:
        assert preview.counts == {"Invalid": 1}
    assert not list((tmp_path / "out").iterdir())


def test_ui_cancellation_waits_for_encoder_then_cleans_up(window, tmp_path, monkeypatch):
    import media_tool.images as images
    source = fixture_image(tmp_path / "original.jpg")
    output = tmp_path / "out"
    output.mkdir()
    entered, release = threading.Event(), threading.Event()
    original_encode = images.encode
    def delayed_encode(*args):
        entered.set()
        assert release.wait(5)
        return original_encode(*args)
    monkeypatch.setattr(images, "encode", delayed_encode)
    window.destination.setText(str(output))
    window.start_scan([source.path])
    wait_until(lambda: not window.busy)
    window.start_rename_button.click()
    try:
        wait_until(entered.is_set)
        assert window.visual.activity.cancel.isEnabled()
        window.visual.activity.cancel.click()
        assert window.jobs_dialog.worker.cancelled.is_set()
        assert window.busy
    finally:
        release.set()
    wait_until(lambda: not window.busy)
    assert window.result_model.items[0]["state"] == "cancelled"
    assert window.model.rowCount() == 1
    assert not list(output.iterdir())


def test_schema_three_migration_retains_old_jobs(tmp_path):
    store = Store(tmp_path / "history")
    job = store.enqueue("Waiting", "sample", [{"name": "old", "seconds": 0}])
    store.db.execute("PRAGMA user_version=3")
    store.close()
    store = Store(tmp_path / "history")
    try:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA
        assert store.items(job)[0]["state"] == "queued"
    finally:
        store.close()


def test_comparison_close_and_main_close_wait_for_decoder(window, tmp_path, monkeypatch):
    from media_tool.image_compare import CompareWorker, CompareDialog
    import time
    def slow_decode(worker):
        while not worker.isInterruptionRequested():
            time.sleep(0.005)
    monkeypatch.setattr(CompareWorker, "run", slow_decode)
    dialog = CompareDialog({"source": "unused", "output": "unused"}, window)
    window.comparison_dialog = dialog
    dialog.destroyed.connect(lambda: setattr(window, "comparison_dialog", None))
    dialog.show()
    window.close()
    wait_until(lambda: window.comparison_dialog is None and not window.isVisible())


def test_palette_transparency_is_preserved(tmp_path):
    source = tmp_path / "palette.png"
    with Image.new("P", (12, 8), 0) as image:
        image.putpalette([255, 0, 0] * 256)
        image.save(source, transparency=0)
    preview = plan(tmp_path, [FileRecord.from_path(source)], "png")
    assert run(tmp_path, preview)[0]["state"] == "succeeded"
    with Image.open(preview.items[0].output) as output:
        assert output.convert("RGBA").getpixel((3, 3))[3] == 0
