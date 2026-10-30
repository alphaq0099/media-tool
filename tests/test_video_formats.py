import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from media_tool.domain import FileRecord
from media_tool.jobs import SCHEMA, Store, execute_job
from media_tool.planning import PlanRequest
from media_tool.video import plan_video, probe, video_snapshot, verify_output, encode_command
from test_foundation import app, window, wait_until
from test_video import media_tools, clip, run_plan


def plan(records, container="mp4", codec="hevc", **options):
    return plan_video(records, PlanRequest("video", extension=container, video_codec=codec, **options))


@pytest.mark.parametrize("container,codec,audio,preset", [
    (container, codec, audio, preset)
    for container in ("mp4", "mkv") for codec in ("h264", "hevc")
    for audio, preset in ((0, "superfast"), (1, "veryfast"), (1, "medium"))])
def test_formats_encode_decode_and_preserve_source(tmp_path, media_tools, container, codec, audio, preset):
    source = clip(tmp_path / "original 日本.mp4", audio=audio)
    before = Path(source.path).read_bytes()
    preview = plan([source], container, codec, video_preset=preset, resize_mode="percent", resize_percent=50)
    assert preview.counts == {"Planned": 1}, preview.items
    item = run_plan(tmp_path, preview)[0]
    assert item["state"] == "succeeded", item
    output = Path(preview.items[0].output)
    assert output.suffix == "." + container
    info = probe(output, media_tools)
    assert info["streams"][0]["codec_name"] == codec
    assert [info["streams"][0][axis] for axis in ("width", "height")] == [80, 44]
    assert len(info["streams"]) == 1 + audio
    assert Path(source.path).read_bytes() == before
    if codec == "hevc" and container == "mp4":
        raw = json.loads(subprocess.check_output([media_tools["ffprobe"], "-v", "error", "-show_streams", "-of", "json", str(output)],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))
        assert raw["streams"][0]["codec_tag_string"] == "hvc1"
    assert not list(tmp_path.glob("*.part"))
    bad = copy.deepcopy(info)
    bad["streams"][0]["codec_name"] = "hevc" if codec == "h264" else "h264"
    with pytest.raises(ValueError, match="codec"):
        verify_output(preview.items[0].video_spec, bad)
    bad = copy.deepcopy(info)
    bad["format"] = "mp4" if container == "mkv" else "matroska"
    with pytest.raises(ValueError, match="container"):
        verify_output(preview.items[0].video_spec, bad)


@pytest.mark.parametrize("quality,expected", [(28, 32), (23, 28), (20, 24), (18, 20)])
def test_hevc_quality_uses_its_own_scale(tmp_path, media_tools, quality, expected):
    source = clip(tmp_path / "source.mp4", audio=0)
    preview = plan([source], video_crf=quality, video_threads=1)
    spec = preview.items[0].video_spec
    assert spec["crf"] == expected
    args = encode_command(spec, tmp_path / "unused.part")
    assert "pools=none:frame-threads=1:log-level=error" in args
    assert run_plan(tmp_path, preview)[0]["state"] == "succeeded"


def test_unsupported_options_missing_encoder_and_muxer_are_actionable(tmp_path, media_tools, monkeypatch):
    import media_tool.video as video
    assert plan([], video_mode="target_size").counts == {}
    with pytest.raises(ValueError, match="container"):
        plan([], container="webm")
    with pytest.raises(ValueError, match="H.264 or H.265"):
        plan([], codec="vp9")
    real = video.run_process
    def missing(args, *pos, **kwargs):
        result = real(args, *pos, **kwargs)
        return "\n".join(line for line in result.splitlines() if "libx265" not in line) if "-encoders" in args else result
    monkeypatch.setattr(video, "run_process", missing)
    with pytest.raises(ValueError, match="libx265"):
        plan([], codec="hevc")
    assert video.inspect_tools()["aac"]  # H.264 does not require x265.
    monkeypatch.setattr(video, "run_process", lambda args, *pos, **kw: "" if "-muxers" in args else real(args, *pos, **kw))
    with pytest.raises(ValueError, match="matroska muxer missing"):
        plan([], container="mkv")


@pytest.mark.parametrize("side", [14, 16])
def test_hevc_minimum_dimensions(tmp_path, media_tools, side):
    source = clip(tmp_path / "source.mp4", audio=0, size="64x64")
    preview = plan([source], resize_mode="dimensions", resize_width=side, resize_height=side)
    if side < 16:
        assert preview.counts == {"Invalid": 1}
        assert "at least 16 pixels" in preview.items[0].reason
    else:
        item = run_plan(tmp_path, preview)[0]
        assert item["state"] == "succeeded", item


@pytest.mark.parametrize("audio", [0, 1])
def test_mkv_h264_two_pass_target_size(tmp_path, media_tools, audio):
    source = clip(tmp_path / "original.mp4", audio=audio, duration=2)
    preview = plan([source], "mkv", "h264", video_mode="target_size", video_target_size=0.1)
    item = run_plan(tmp_path, preview)[0]
    assert item["state"] == "succeeded", item
    assert "Target met:" in item["message"]
    assert Path(preview.items[0].output).stat().st_size <= 100_000
    assert not list((tmp_path / "history").glob(".mediatool-pass-*"))


@pytest.mark.parametrize("point", ["progress", "prepared"])
def test_hevc_cancel_and_retry(tmp_path, media_tools, point):
    source = clip(tmp_path / "original.mp4", audio=0)
    before = Path(source.path).read_bytes()
    preview = plan([source], "mkv")
    store = Store(tmp_path / "history")
    cancel = threading.Event()
    try:
        job = store.enqueue("HEVC", "video", video_snapshot(preview))
        execute_job(store, job, cancelled=cancel.is_set, hook=lambda stage: cancel.set() if stage == point else None)
        assert store.items(job)[0]["state"] == "cancelled", store.items(job)
        assert not Path(preview.items[0].output).exists()
        assert not list(tmp_path.glob("*.part"))
        store.retry(job)
        assert execute_job(store, job) == "succeeded", store.items(job)
        assert Path(source.path).read_bytes() == before
    finally:
        store.close()


@pytest.mark.parametrize("container,codec", [("mp4", "hevc"), ("mkv", "hevc"), ("mkv", "h264")])
def test_vfr_frame_count_and_timestamps(tmp_path, media_tools, container, codec):
    source = tmp_path / "vfr.mp4"
    subprocess.run([media_tools["ffmpeg"], "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=60",
        "-vf", "select='if(lt(t,0.5),1,not(mod(n,3)))'", "-t", "2", "-fps_mode:v", "passthrough",
        "-enc_time_base:v", "1:30000", "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1",
        "-pix_fmt", "yuv420p", "-bsf:v", "h264_metadata=tick_rate=60:fixed_frame_rate_flag=0", str(source)],
        check=True, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    preview = plan([FileRecord.from_path(source)], container, codec)
    item = run_plan(tmp_path, preview)[0]
    assert item["state"] == "succeeded", item
    timestamps = []
    for path in (source, preview.items[0].output):
        data = json.loads(subprocess.check_output([media_tools["ffprobe"], "-v", "error", "-select_streams", "v:0",
            "-show_packets", "-show_entries", "packet=pts_time,dts_time", "-of", "json", str(path)],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))
        timestamps.append(sorted(float(p["pts_time"]) for p in data["packets"]))
        dts = [float(p["dts_time"]) for p in data["packets"] if "dts_time" in p]
        assert all(b > a for a, b in zip(dts, dts[1:]))
    assert len(timestamps[0]) == len(timestamps[1]) > 30
    # FFmpeg's Matroska muxer stores timestamps at millisecond precision.
    tolerance = .0011 if container == "mkv" else .0001
    assert max(abs((a-timestamps[0][0])-(b-timestamps[1][0])) for a, b in zip(*timestamps)) < tolerance


def test_ui_format_codec_and_target_mode(window, tmp_path, media_tools):
    source = clip(tmp_path / "source.mp4", audio=1)
    window._tool_changed("Video")
    assert window.option_title.text() == "Container"
    controls = window.video_format_controls
    mode = window.compression_controls.mode
    mode.setCurrentIndex(mode.findData("target_size"))
    window.format.setCurrentText("MKV")
    assert not window.detail.isVisible()
    controls.codec.setCurrentIndex(controls.codec.findData("hevc"))
    assert mode.currentData() == "target_size"
    assert not window.detail.isVisible() and not controls.note.isVisible()
    window.visual.groups['Advanced'].help.update_tip()
    assert controls.note.text() in window.visual.groups['Advanced'].help.toolTip()
    assert mode.model().item(mode.findData("target_size")).isEnabled()
    mode.setCurrentIndex(mode.findData("quality"))
    window.model.append([source])
    window.start_rename_button.click()
    wait_until(lambda: not window.busy and not window.jobs_dialog.enabled, timeout=30000)
    assert window.result_model.items[0]["state"] == "succeeded", window.status.text()
    output = window.result_model.items[0]["output"]
    assert output.endswith(".mkv")
    assert probe(output, media_tools)["streams"][0]["codec_name"] == "hevc"
    controls.codec.setCurrentIndex(0)
    assert mode.model().item(mode.findData("target_size")).isEnabled()
    window._tool_changed("Images")
    assert not controls.panel.isVisible()
    assert window.option_title.text() == "Format"


def test_schema8_legacy_video_defaults_survive_upgrade(tmp_path, media_tools):
    source = clip(tmp_path / "legacy.mp4", audio=0)
    specs = list(video_snapshot(plan([source], codec="h264")))
    for key in ("codec", "container"):
        del specs[0][key]
    store = Store(tmp_path / "history")
    job = store.enqueue("Legacy", "video", specs)
    store.db.execute("PRAGMA user_version=8")
    store.close()
    store = Store(tmp_path / "history")
    try:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA
        assert execute_job(store, job) == "succeeded", store.items(job)
        assert probe(specs[0]["output"], media_tools)["streams"][0]["codec_name"] == "h264"
    finally:
        store.close()


@pytest.mark.parametrize("point", ["written", "prepared", "committed"])
def test_hevc_process_loss_and_recovery(tmp_path, media_tools, point):
    source = clip(tmp_path / "source.mp4", audio=0)
    before = Path(source.path).read_bytes()
    preview = plan([source], "mkv")
    store = Store(tmp_path / "history")
    try:
        job = store.enqueue("HEVC", "video", video_snapshot(preview))
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
            env=env, timeout=30, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        assert result.returncode == 73, result.stderr.decode(errors="replace")
        assert store.recover() == 1
        assert store.retry(job) == 1
        assert execute_job(store, job) == "succeeded", store.items(job)
        assert Path(source.path).read_bytes() == before
        assert not list(tmp_path.glob("*.part"))
    finally:
        store.close()


def test_hevc_late_collision_preserves_both_existing_files(tmp_path, media_tools):
    source = clip(tmp_path / "source.mp4", audio=0)
    before = Path(source.path).read_bytes()
    preview = plan([source], "mkv")
    output = Path(preview.items[0].output)
    item = run_plan(tmp_path, preview, hook=lambda stage: output.write_bytes(b"keep this") if stage == "prepared" else None)[0]
    assert item["state"] == "failed"
    assert output.read_bytes() == b"keep this"
    assert Path(source.path).read_bytes() == before
    assert not list(tmp_path.glob("*.part"))
