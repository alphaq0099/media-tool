import json
from pathlib import Path
import threading

import pytest
from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from media_tool.images import plan_images, image_snapshot
from media_tool.jobs import SCHEMA, Store, execute_job
from media_tool.planning import PlanRequest
from media_tool.resizing import resize_dimensions
from test_foundation import app, window, wait_until
from test_images import fixture_image, plan, run


@pytest.mark.parametrize("size,options,expected", [
    ((4000, 3000), {}, (1440, 1080)),
    ((3000, 4000), {}, (810, 1080)),
    ((100, 80), {}, (100, 80)),
    ((100, 80), {"no_upscale": False}, (1350, 1080)),
    ((100, 80), {"aspect": False}, (100, 80)),
    ((100, 80), {"aspect": False, "no_upscale": False}, (1920, 1080)),
    ((4000, 3000), {"width": 1000, "height": 1000}, (1000, 750)),
    ((1, 40000), {"width": 10, "height": 10}, (1, 10)),
    ((3, 5), {"width": 2, "height": 2}, (1, 2)),
    ((3, 5), {"mode": "percent", "percent": 50}, (2, 3)),
    ((1, 1), {"mode": "percent", "percent": 1}, (1, 1)),
    ((100, 50), {"mode": "percent", "percent": 200}, (100, 50)),
    ((100, 50), {"mode": "percent", "percent": 200, "no_upscale": False}, (200, 100)),
    ((2000, 1000), {"width": 1000, "height": 2000, "aspect": False}, (1000, 1000)),
])
def test_geometry_is_deterministic(size, options, expected):
    assert resize_dimensions(size, **({"mode": "dimensions"} | options)) == expected


@pytest.mark.parametrize("options", [dict(mode="bad"), dict(mode="percent", percent=0),
    dict(mode="dimensions", width=0), dict(mode="dimensions", width=40001),
    dict(mode="percent", percent=401), dict(mode="dimensions", width=40000, height=40000, aspect=False, no_upscale=False)])
def test_invalid_or_oversized_results_are_rejected(options):
    with pytest.raises(ValueError):
        resize_dimensions((100, 100), **options)


@pytest.mark.parametrize("extension", ["jpg", "png", "webp"])
@pytest.mark.parametrize("mode", ["dimensions", "percent"])
def test_resize_encode_matches_preview_and_keeps_original(tmp_path, extension, mode):
    source = fixture_image(tmp_path / "source.png", "RGBA", (50, 100, 150, 100), size=(160, 80))
    before = Path(source.path).read_bytes()
    preview = plan(tmp_path, [source], extension, resize_mode=mode, resize_width=80, resize_height=80, resize_percent=50)
    assert preview.items[0].image_spec["output_size"] == [80, 40]
    items = run(tmp_path, preview)
    assert items[0]["state"] == "succeeded", items
    with Image.open(preview.items[0].output) as output:
        output.load()
        assert output.size == (80, 40)
        if extension != "jpg":
            assert output.convert("RGBA").getpixel((30, 20))[3] == 100
    assert Path(source.path).read_bytes() == before
    assert "160 × 80 → 80 × 40" in items[0]["message"]


def test_orientation_precedes_resize_and_metadata_dimensions_are_updated(tmp_path):
    exif = Image.Exif()
    exif[274], exif[256], exif[257], exif[315] = 6, 120, 80, "Fixture"
    exif[34665] = {40962: 120, 40963: 80}
    source = fixture_image(tmp_path / "rotated.jpg", size=(120, 80), exif=exif)
    preview = plan(tmp_path, [source], "png", resize_mode="dimensions", resize_width=60, resize_height=60, metadata="preserve")
    assert preview.items[0].image_spec["image"]["output_size"] == [80, 120]
    assert preview.items[0].image_spec["output_size"] == [40, 60]
    items = run(tmp_path, preview)
    assert items[0]["state"] == "succeeded", items
    with Image.open(preview.items[0].output) as output:
        assert output.size == (40, 60)
        meta = output.getexif()
        assert meta.get(274, 1) == 1
        assert (meta[256], meta[257], meta[315]) == (40, 60, "Fixture")
        nested = meta.get_ifd(34665)
        assert (nested[40962], nested[40963]) == (40, 60)


def test_unlocked_dimensions_are_exact_when_upscale_allowed(tmp_path):
    source = fixture_image(tmp_path / "source.png", size=(100, 50))
    preview = plan(tmp_path, [source], "png", resize_mode="dimensions", resize_width=40, resize_height=80,
                   resize_aspect=False, resize_no_upscale=False)
    assert "Aspect ratio unlocked" in preview.items[0].reason
    assert run(tmp_path, preview)[0]["state"] == "succeeded"
    with Image.open(preview.items[0].output) as output:
        assert output.size == (40, 80)


def test_large_upscale_blocks_plan_before_output_creation(tmp_path):
    source = fixture_image(tmp_path / "source.png", size=(2000, 1500))
    preview = plan(tmp_path, [source], resize_mode="percent", resize_percent=400, resize_no_upscale=False)
    assert preview.counts == {"Invalid": 1}
    assert "40 megapixel" in preview.items[0].reason
    assert not list((tmp_path / "out").iterdir())


def test_resize_cancellation_and_saved_retry(tmp_path):
    source = fixture_image(tmp_path / "source.png", size=(120, 80))
    preview = plan(tmp_path, [source], resize_mode="percent", resize_percent=25)
    store = Store(tmp_path / "history")
    cancel = threading.Event()
    try:
        job = store.enqueue("Resize", "images", image_snapshot(preview))
        execute_job(store, job, cancelled=cancel.is_set, hook=lambda point: cancel.set() if point == "written" else None)
        assert store.items(job)[0]["state"] == "cancelled"
        assert not list((tmp_path / "out").iterdir())
    finally:
        store.close()
    store = Store(tmp_path / "history")
    try:
        store.retry(job)
        assert execute_job(store, job) == "succeeded"
        with Image.open(preview.items[0].output) as output:
            assert output.size == (30, 20)
    finally:
        store.close()


def test_schema_four_conversion_without_resize_fields_still_runs(tmp_path):
    source = fixture_image(tmp_path / "source.png")
    preview = plan(tmp_path, [source])
    specs = list(image_snapshot(preview))
    specs[0].pop("output_size")
    specs[0].pop("resize")
    store = Store(tmp_path / "history")
    job = store.enqueue("Older conversion", "images", specs)
    store.db.execute("PRAGMA user_version=4")
    store.close()
    store = Store(tmp_path / "history")
    try:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA
        assert execute_job(store, job) == "succeeded"
        with Image.open(preview.items[0].output) as output:
            assert output.size == (24, 16)
    finally:
        store.close()


def test_live_preview_updates_with_settings_selection_and_actual_output(window, tmp_path):
    first = fixture_image(tmp_path / "landscape.png", size=(160, 80))
    second = fixture_image(tmp_path / "portrait.png", size=(80, 160))
    window.model.append([first, second])
    resize = window.resize_controls
    resize.mode.setCurrentIndex(1)
    resize.width.setValue(80)
    resize.height.setValue(80)
    wait_until(lambda: resize.preview.text() == "First of 2 files\n160 × 80 → 80 × 40")
    window.table.selectRow(1)
    wait_until(lambda: resize.preview.text() == "80 × 160 → 40 × 80")
    resize.mode.setCurrentIndex(2)
    resize.percent.setValue(25)
    wait_until(lambda: resize.preview.text() == "80 × 160 → 20 × 40")
    window.preview_changes()
    wait_until(lambda: not window.busy)
    assert window.plan_model.data(window.plan_model.index(0, 3)) == "80×160 → 20×40"
    resize.percent.setValue(50)
    assert window.last_plan is None
    window.start_rename_button.click()
    wait_until(lambda: not window.busy)
    assert window.result_model.items[0]["state"] == "succeeded", window.status.text()
    with Image.open(window.result_model.items[0]["output"]) as output:
        assert output.size == (40, 80)
    assert window.model.rowCount() == 1


def test_close_waits_for_dimension_worker(window, tmp_path, monkeypatch):
    from media_tool.resize_ui import DimensionWorker
    import time
    def slow(worker):
        while not worker.isInterruptionRequested():
            time.sleep(0.005)
    monkeypatch.setattr(DimensionWorker, "run", slow)
    window.model.append([fixture_image(tmp_path / "photo.png")])
    window.resize_controls.mode.setCurrentIndex(1)
    wait_until(lambda: window.resize_controls.worker is not None)
    window.close()
    wait_until(lambda: window.resize_controls.worker is None and not window.isVisible())
