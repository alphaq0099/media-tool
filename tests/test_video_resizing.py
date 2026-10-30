import json
from pathlib import Path
import subprocess
import threading

import pytest

from media_tool.domain import FileRecord
from media_tool.jobs import SCHEMA, Store, execute_job
from media_tool.video import probe, video_snapshot, verify_output
from media_tool.video_geometry import video_dimensions
from test_foundation import app, window, wait_until
from test_video import media_tools, clip, make_plan, run_plan


def stream(width=1920, height=1080, sar="1:1", rotation=0):
    return dict(width=width, height=height, sample_aspect_ratio=sar, rotation=rotation)


@pytest.mark.parametrize("source,options,expected", [
    (stream(), {}, (1920, 1080)),
    (stream(), dict(mode="dimensions", width=1280, height=720), (1280, 720)),
    (stream(1080, 1920), dict(mode="dimensions", width=1280, height=720), (404, 720)),
    (stream(1000, 1000), dict(mode="dimensions", width=640, height=480), (480, 480)),
    (stream(320, 180), dict(mode="dimensions"), (320, 180)),
    (stream(320, 180), dict(mode="dimensions", no_upscale=False), (1920, 1080)),
    (stream(), dict(mode="percent", percent=50), (960, 540)),
    (stream(320, 180), dict(mode="percent", percent=200), (320, 180)),
    (stream(320, 180), dict(mode="percent", percent=200, no_upscale=False), (640, 360)),
    (stream(161, 91), {}, (160, 90)),
    (stream(320, 180, rotation=90), dict(mode="dimensions", width=160, height=160), (90, 160)),
    (stream(320, 180, rotation=-90), {}, (180, 320)),
    (stream(320, 180, rotation=180), {}, (320, 180)),
    (stream(720, 576, sar="16:15"), {}, (768, 576)),
    (stream(720, 576, sar="16:15", rotation=90), {}, (576, 768)),
    (stream(720, 576, sar="16:15"), dict(mode="dimensions", width=640, height=480), (640, 480)),
    (stream(720, 576, sar="8:9"), {}, (640, 576)),
])
def test_display_aware_even_dimensions(source, options, expected):
    assert video_dimensions(source, **options) == expected


@pytest.mark.parametrize("source,options", [
    (stream(), dict(mode="bad")),
    (stream(), dict(mode="percent", percent=0)),
    (stream(), dict(mode="dimensions", width=0)),
    (stream(), dict(mode="dimensions", aspect=False)),
    (stream(160, 90), dict(mode="percent", percent=1)),
    (stream(8000, 4000), dict(mode="percent", percent=400, no_upscale=False)),
    (stream(sar="broken"), {}), (stream(sar="1:0"), {}), (stream(sar="-1:1"), {}),
    (stream(rotation=45), {}), (stream(sar="100000:1"), {}),
    (stream(2, 40000), dict(mode="dimensions", width=2, height=2)),
])
def test_invalid_video_resize_is_rejected(source, options):
    with pytest.raises(ValueError):
        video_dimensions(source, **options)


def run_ffmpeg(tools, args):
    subprocess.run([tools["ffmpeg"], "-hide_banner", "-v", "error", *map(str, args)],
                   check=True, capture_output=True, timeout=30,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def special_clip(tmp_path, tools, kind):
    base = tmp_path / f"{kind}.mp4"
    if kind == "odd":
        run_ffmpeg(tools, ["-f", "lavfi", "-i", "testsrc=size=161x91:rate=12", "-t", "0.5",
            "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv444p", base])
    elif kind in ("sar", "rotated-sar"):
        run_ffmpeg(tools, ["-f", "lavfi", "-i", "testsrc2=size=160x120:rate=12", "-vf", "setsar=4/3", "-t", "0.5",
            "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", base])
    else:
        clip(base, audio=1 if kind == "rotated" else 0, size="160x90")
    if kind in ("rotated", "rotated-sar", "upside-down"):
        rotated = tmp_path / f"{kind}-tagged.mp4"
        run_ffmpeg(tools, ["-display_rotation:v:0", "180" if kind == "upside-down" else "90",
                          "-i", base, "-c", "copy", rotated])
        base = rotated
        video = probe(base, tools)["streams"][0]
        assert abs(video["rotation"]) in (90, 180), video
    return FileRecord.from_path(base)


@pytest.mark.parametrize("kind,expected", [("odd", [160, 90]), ("sar", [212, 120]),
    ("rotated", [90, 160]), ("rotated-sar", [120, 212]), ("upside-down", [160, 90])])
def test_actual_orientation_and_pixel_shape_are_normalized(tmp_path, media_tools, kind, expected):
    source = special_clip(tmp_path, media_tools, kind)
    before = Path(source.path).read_bytes()
    plan = make_plan([source])
    assert plan.counts == {"Planned": 1}, plan.items
    assert plan.items[0].video_spec["output_size"] == expected
    items = run_plan(tmp_path, plan)
    assert items[0]["state"] == "succeeded", items
    video = probe(plan.items[0].output, media_tools)["streams"][0]
    assert [video["width"], video["height"]] == expected
    assert video["sample_aspect_ratio"] == "1:1"
    assert video["rotation"] == 0
    # Compare decoded pixels to FFmpeg's metadata-aware display transform, not just dimensions.
    def pixels(path, filters):
        return subprocess.check_output([media_tools["ffmpeg"], "-v", "error", "-i", str(path), "-vf", filters,
            "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    reference = pixels(source.path, f"scale={expected[0]}:{expected[1]}:flags=lanczos,setsar=1")
    actual = pixels(plan.items[0].output, "null")
    assert len(actual) == len(reference) == expected[0] * expected[1] * 3
    assert sum(abs(a-b) for a,b in zip(actual, reference)) / len(actual) < 12
    assert Path(source.path).read_bytes() == before


@pytest.mark.parametrize("size", ["320x180", "180x320", "180x180"])
def test_actual_resize_matches_preview_bounds_and_keeps_source(tmp_path, media_tools, size):
    source = clip(tmp_path / "source.mp4", size=size, audio=0)
    before = Path(source.path).read_bytes()
    plan = make_plan([source], resize_mode="dimensions", resize_width=160, resize_height=120)
    spec = plan.items[0].video_spec
    assert max(spec["output_size"]) <= 160 and spec["output_size"][1] <= 120
    assert run_plan(tmp_path, plan)[0]["state"] == "succeeded"
    output = probe(plan.items[0].output, media_tools)["streams"][0]
    assert [output["width"], output["height"]] == spec["output_size"]
    assert Path(source.path).read_bytes() == before


def test_saved_resize_survives_cancel_retry_and_later_control_changes(window, tmp_path, media_tools):
    source = clip(tmp_path / "clip.mp4", audio=0)
    window._tool_changed("Video")
    resize = window.resize_controls
    resize.mode.setCurrentIndex(resize.mode.findData("percent"))
    resize.percent.setValue(50)
    plan = make_plan([source], **resize.options())
    store = Store(tmp_path / "history")
    cancel = threading.Event()
    try:
        job = store.enqueue("Resize", "video", video_snapshot(plan))
        execute_job(store, job, cancelled=cancel.is_set, hook=lambda point: cancel.set() if point == "written" else None)
        assert store.items(job)[0]["state"] == "cancelled"
        assert not Path(plan.items[0].output).exists()
    finally:
        store.close()
    resize.percent.setValue(25)
    store = Store(tmp_path / "history")
    try:
        store.retry(job)
        assert execute_job(store, job) == "succeeded"
        output = probe(plan.items[0].output, media_tools)["streams"][0]
        assert [output["width"], output["height"]] == [80, 44]
        assert json.loads(store.items(job)[0]["spec"])["resize"]["percent"] == 50
    finally:
        store.close()


def test_schema_six_video_without_resize_fields_still_converts(tmp_path, media_tools):
    source = clip(tmp_path / "old.mp4", audio=0)
    plan = make_plan([source])
    spec = dict(video_snapshot(plan)[0])
    spec.pop("resize")
    spec.pop("display_size")
    store = Store(tmp_path / "history")
    job = store.enqueue("Older video", "video", [spec])
    store.db.execute("PRAGMA user_version=6")
    store.close()
    store = Store(tmp_path / "history")
    try:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA
        assert execute_job(store, job) == "succeeded"
        assert probe(plan.items[0].output, media_tools)["streams"][0]["width"] == 160
    finally:
        store.close()


def test_video_live_preview_and_settings_stay_separate_from_images(window, tmp_path, media_tools):
    resize = window.resize_controls
    resize.mode.setCurrentIndex(resize.mode.findData("dimensions"))
    resize.aspect.setChecked(False)
    resize.width.setValue(75)
    window._tool_changed("Video")
    assert resize.mode.currentData() == "none"
    assert resize.aspect.isChecked() and not resize.aspect.isEnabled()
    source = special_clip(tmp_path, media_tools, "rotated")
    window.model.append([source])
    resize.mode.setCurrentIndex(resize.mode.findData("dimensions"))
    resize.width.setValue(80)
    resize.height.setValue(80)
    wait_until(lambda: resize.preview.text() == "90 × 160 → 44 × 80", timeout=15000)
    window.preview_changes()
    wait_until(lambda: not window.busy, timeout=15000)
    assert window.plan_model.data(window.plan_model.index(0, 3)) == "90×160 → 44×80"
    assert window.start_rename_button.text() == "Start Resize"
    resize.width.setValue(60)
    assert window.last_plan is None
    window._tool_changed("Images")
    assert resize.width.value() == 75 and not resize.aspect.isChecked() and resize.aspect.isEnabled()
    window._tool_changed("Video")
    assert resize.width.value() == 60 and resize.aspect.isChecked() and not resize.aspect.isEnabled()


def test_verification_rejects_wrong_output_display_transform(tmp_path, media_tools):
    import copy
    source = clip(tmp_path / "clip.mp4", audio=0)
    spec = make_plan([source]).items[0].video_spec
    for change in ({"rotation": 90}, {"sample_aspect_ratio": "4:3"}):
        info = copy.deepcopy(spec["video"])
        info["streams"][0].update(change)
        with pytest.raises(ValueError, match="orientation or pixel aspect"):
            verify_output(spec, info)


def test_mirrored_display_matrix_is_explicitly_blocked(tmp_path, media_tools):
    source = clip(tmp_path / "original.mp4", audio=0)
    mirrored = tmp_path / "mirrored.mp4"
    run_ffmpeg(media_tools, ["-display_hflip:v:0", "-i", source.path, "-c", "copy", mirrored])
    plan = make_plan([FileRecord.from_path(mirrored)])
    assert plan.counts == {"Invalid": 1}
    assert "Mirrored" in plan.items[0].reason
    assert not Path(plan.items[0].output).exists()


@pytest.mark.parametrize("no_upscale,expected", [(True, [160, 90]), (False, [320, 180])])
def test_actual_percentage_enlargement_requires_opt_in(tmp_path, media_tools, no_upscale, expected):
    source = clip(tmp_path / "small.mp4", audio=0)
    plan = make_plan([source], resize_mode="percent", resize_percent=200, resize_no_upscale=no_upscale)
    assert plan.items[0].video_spec["output_size"] == expected
    assert run_plan(tmp_path, plan)[0]["state"] == "succeeded"
    output = probe(plan.items[0].output, media_tools)["streams"][0]
    assert [output["width"], output["height"]] == expected
