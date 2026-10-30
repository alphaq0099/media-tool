import array
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
from media_tool.video import inspect_tools, probe, video_snapshot, verify_output
from media_tool.video_formats import verify_webm_header
from test_foundation import app, window, wait_until
from test_video import media_tools, clip, run_plan
from test_video_formats import plan


def webm(records, **options):
    return plan(records, "webm", "vp9", **options)


def command(args):
    return subprocess.run(args, check=True, capture_output=True, timeout=40,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout


@pytest.mark.parametrize("extension,audio", [("mp4", 1), ("mkv", 0), ("mov", 1), ("webm", 1)])
def test_real_webm_conversion_and_source_protection(tmp_path, media_tools, extension, audio):
    source = clip(tmp_path / f"source 日本 [1].{extension}", audio=audio)
    before = Path(source.path).read_bytes()
    preview = webm([source], resize_mode="percent", resize_percent=50)
    assert preview.counts == {"Planned": 1}, preview.items
    item = run_plan(tmp_path, preview)[0]
    assert item["state"] == "succeeded", item
    path = Path(preview.items[0].output)
    assert path.suffix == ".webm" and path != Path(source.path)
    verify_webm_header(path)
    info = probe(path, media_tools, audio_layout=True)
    assert info["streams"][0]["codec_name"] == "vp9"
    assert [info["streams"][0][key] for key in ("width", "height")] == [80, 44]
    assert len(info["streams"]) == 1 + audio
    if audio:
        assert info["streams"][1]["codec_name"] == "opus"
        assert info["streams"][1]["sample_rate"] == "48000"
        assert "Opus" in item["message"]
        broken = copy.deepcopy(info)
        broken["streams"][1]["codec_name"] = "aac"
        with pytest.raises(ValueError, match="audio codec"):
            verify_output(preview.items[0].video_spec, broken)
        broken = copy.deepcopy(info)
        broken["streams"][1]["sample_rate"] = "44100"
        with pytest.raises(ValueError, match="sample rate"):
            verify_output(preview.items[0].video_spec, broken)
    assert Path(source.path).read_bytes() == before
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize("quality,crf,preset", [(28,40,"superfast"),(23,32,"veryfast"),(20,26,"medium"),(18,20,"superfast")])
def test_vp9_quality_speed_presets(tmp_path, media_tools, quality, crf, preset):
    source = clip(tmp_path / "source.mp4", audio=0)
    preview = webm([source], video_crf=quality, video_preset=preset, video_threads=1)
    assert preview.items[0].video_spec["crf"] == crf
    item = run_plan(tmp_path, preview)[0]
    assert item["state"] == "succeeded", item


@pytest.mark.parametrize("layout,channels", [("stereo",2),("3.0",3),("quad",4),("5.0",5),("5.1",6)])
def test_opus_resampling_and_channel_preservation(tmp_path, media_tools, layout, channels):
    source = tmp_path / "audio.mkv"
    command([media_tools["ffmpeg"], "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=12",
        "-f", "lavfi", "-i", f"anullsrc=r=44100:cl={layout}", "-t", "1", "-c:v", "libx264", "-threads", "1",
        "-c:a", "flac", str(source)])
    preview = webm([FileRecord.from_path(source)])
    assert preview.counts == {"Planned": 1}, preview.items
    assert "resampled from 44100 Hz" in preview.items[0].reason
    item = run_plan(tmp_path, preview)[0]
    assert item["state"] == "succeeded", item
    audio = probe(preview.items[0].output, media_tools, audio_layout=True)["streams"][1]
    assert audio["channel_layout"] == layout
    assert audio["channels"] == channels
    assert audio["sample_rate"] == "48000"


def test_unsupported_opus_layout_blocks_before_encoding(tmp_path, media_tools):
    source = tmp_path / "2.1.mkv"
    command([media_tools["ffmpeg"], "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=12",
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=2.1", "-t", "1", "-c:v", "libx264", "-threads", "1", "-c:a", "pcm_s16le", str(source)])
    preview = webm([FileRecord.from_path(source)])
    assert preview.counts == {"Invalid": 1}
    assert "not silently downmixed" in preview.items[0].reason


def test_opus_track_selection_and_omissions(tmp_path, media_tools):
    source = clip(tmp_path / "tracks.mkv", audio=2)
    assert webm([source], video_audio=2).counts == {"Review": 1}
    preview = webm([source], video_audio=2, video_allow_omissions=True)
    assert run_plan(tmp_path, preview)[0]["state"] == "succeeded"
    raw = command([media_tools["ffmpeg"], "-v", "error", "-i", preview.items[0].output,
        "-map", "0:a:0", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"])
    samples = array.array("h", raw)[4000:12000]
    crossings = sum(a < 0 <= b for a, b in zip(samples, samples[1:]))
    assert 850 < crossings * 2 < 910


@pytest.mark.parametrize("point", ["progress", "prepared"])
def test_webm_cancel_and_retry(tmp_path, media_tools, point):
    source = clip(tmp_path / "source.mp4", audio=1)
    before = Path(source.path).read_bytes()
    preview = webm([source])
    store = Store(tmp_path / "history")
    cancel = threading.Event()
    try:
        job = store.enqueue("WebM", "video", video_snapshot(preview))
        execute_job(store, job, cancelled=cancel.is_set, hook=lambda stage: cancel.set() if stage == point else None)
        assert store.items(job)[0]["state"] == "cancelled", store.items(job)
        assert not Path(preview.items[0].output).exists()
        assert not list(tmp_path.glob("*.part"))
        store.retry(job)
        assert execute_job(store, job) == "succeeded", store.items(job)
        assert Path(source.path).read_bytes() == before
    finally:
        store.close()


def test_webm_vfr_keeps_frames_and_relative_timestamps(tmp_path, media_tools):
    source = tmp_path / "vfr.mp4"
    command([media_tools["ffmpeg"], "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=60",
        "-vf", "select='if(lt(t,0.5),1,not(mod(n,3)))'", "-t", "2", "-fps_mode:v", "passthrough",
        "-enc_time_base:v", "1:30000", "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1",
        "-pix_fmt", "yuv420p", "-bsf:v", "h264_metadata=tick_rate=60:fixed_frame_rate_flag=0", str(source)])
    preview = webm([FileRecord.from_path(source)])
    item = run_plan(tmp_path, preview)[0]
    assert item["state"] == "succeeded", item
    timestamps = []
    for path in (source, preview.items[0].output):
        data = json.loads(command([media_tools["ffprobe"], "-v", "error", "-select_streams", "v:0", "-show_frames",
            "-show_entries", "frame=pts_time", "-of", "json", str(path)]))
        pts = [float(frame["pts_time"]) for frame in data["frames"]]
        assert all(b > a for a, b in zip(pts, pts[1:]))
        timestamps.append(pts)
    assert len(timestamps[0]) == len(timestamps[1]) > 30
    assert max(abs((a-timestamps[0][0])-(b-timestamps[1][0])) for a,b in zip(*timestamps)) < .0011


def test_webm_flash_and_tone_stay_in_sync(tmp_path, media_tools):
    source = clip(tmp_path / "sync.mp4", duration=2, sync=True)
    preview = webm([source])
    item = run_plan(tmp_path, preview)[0]
    assert item["state"] == "succeeded", item
    offsets = []
    for path in (source.path, preview.items[0].output):
        raw = command([media_tools["ffmpeg"], "-v", "error", "-i", path, "-map", "0:v:0", "-vf", "scale=1:1",
            "-pix_fmt", "gray", "-fps_mode", "passthrough", "-f", "rawvideo", "-"])
        visual = next(i for i, value in enumerate(raw) if value > 128) / 12
        sound = command([media_tools["ffmpeg"], "-v", "error", "-i", path, "-map", "0:a:0", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"])
        audible = next(i for i, value in enumerate(array.array("h", sound)) if abs(value) > 2000) / 16000
        offsets.append(audible - visual)
    assert abs(offsets[1] - offsets[0]) < .04


def test_webm_ui_compatibility_and_direct_start(window, tmp_path, media_tools):
    source = clip(tmp_path / "source.mp4")
    window._tool_changed("Video")
    codec = window.video_format_controls.codec
    codec.setCurrentIndex(codec.findData("hevc"))
    window.format.setCurrentText("WebM")
    assert codec.currentData() == "vp9"
    assert not codec.model().item(codec.findData("h264")).isEnabled()
    mode = window.compression_controls.mode
    assert mode.currentData() == "quality"
    assert not mode.model().item(mode.findData("target_size")).isEnabled()
    window.model.append([source])
    window.start_rename_button.click()
    wait_until(lambda: not window.busy and not window.jobs_dialog.enabled, timeout=30000)
    assert window.result_model.items[0]["state"] == "succeeded", window.status.text()
    assert window.result_model.items[0]["output"].endswith(".webm")
    window.format.setCurrentText("MKV")
    assert codec.currentData() == "hevc"
    assert not codec.model().item(codec.findData("vp9")).isEnabled()
    codec.setCurrentIndex(codec.findData("h264"))
    mode.setCurrentIndex(mode.findData("target_size"))
    window.format.setCurrentText("WebM")
    assert mode.currentData() == "quality" and window.detail.isVisible()
    window._tool_changed("Images")
    window._tool_changed("Video")
    assert window.plan_request().extension == "webm" and window.plan_request().video_codec == "vp9"


def test_required_encoders_and_incompatible_combinations(tmp_path, media_tools, monkeypatch):
    import media_tool.video as video
    with pytest.raises(ValueError, match="Target file size"):
        webm([], video_mode="target_size")
    for container, codec in (("webm","h264"),("webm","hevc"),("mp4","vp9"),("mkv","vp9")):
        with pytest.raises(ValueError, match="container"):
            plan([], container, codec)
    real = video.run_process
    def without(name):
        def run(args, *pos, **kw):
            result = real(args, *pos, **kw)
            return "\n".join(line for line in result.splitlines() if name not in line) if "-encoders" in args else result
        return run
    monkeypatch.setattr(video, "run_process", without("libvpx-vp9"))
    with pytest.raises(ValueError, match="libvpx-vp9"):
        webm([])
    monkeypatch.setattr(video, "run_process", without("libopus"))
    source = clip(tmp_path / "source.mp4")
    preview = webm([source])
    assert preview.counts == {"Invalid": 1} and "libopus" in preview.items[0].reason
    silent = clip(tmp_path / "silent.mp4", audio=0)
    assert webm([silent]).counts == {"Planned": 1}
    monkeypatch.setattr(video, "run_process", without(" aac "))
    assert webm([source]).counts == {"Planned": 1}


def test_webm_header_rejects_mkv_and_truncated_files(tmp_path, media_tools):
    source = clip(tmp_path / "ordinary.mkv", audio=0)
    with pytest.raises(ValueError, match="DocType"):
        verify_webm_header(Path(source.path))
    for data in (b"", b"\x1a\x45\xdf\xa3", b"\x1a\x45\xdf\xa3\xff", b"\x1a\x45\xdf\xa3\x82\x42\x82"):
        bad = tmp_path / "broken.webm"
        bad.write_bytes(data)
        with pytest.raises(ValueError):
            verify_webm_header(bad)


@pytest.mark.parametrize("point", ["written", "committed"])
def test_webm_process_loss_and_recovery(tmp_path, media_tools, point):
    source = clip(tmp_path / "source.mp4")
    before = Path(source.path).read_bytes()
    preview = webm([source])
    store = Store(tmp_path / "history")
    try:
        job = store.enqueue("WebM", "video", video_snapshot(preview))
        code = """import os,sys
from media_tool.jobs import Store,execute_job
store=Store(sys.argv[1])
def crash(point):
    if point==sys.argv[3]: os._exit(73)
execute_job(store,sys.argv[2],hook=crash)
"""
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        result = subprocess.run([sys.executable, "-c", code, str(store.directory), job, point], env=env,
            timeout=30, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        assert result.returncode == 73, result.stderr.decode(errors="replace")
        assert store.recover() == store.retry(job) == 1
        assert execute_job(store, job) == "succeeded", store.items(job)
        verify_webm_header(Path(preview.items[0].output))
        assert Path(source.path).read_bytes() == before
        assert not list(tmp_path.glob("*.part"))
    finally:
        store.close()


def test_webm_minimum_size_and_late_collision(tmp_path, media_tools):
    source = clip(tmp_path / "source.mp4", audio=0, size="64x64")
    preview = webm([source], resize_mode="dimensions", resize_width=2, resize_height=2)
    item = run_plan(tmp_path, preview)[0]
    assert item["state"] == "succeeded", item
    preview = webm([source])
    output = Path(preview.items[0].output)
    item = run_plan(tmp_path, preview, hook=lambda stage: output.write_bytes(b"keep me") if stage == "prepared" else None)[0]
    assert item["state"] == "failed"
    assert output.read_bytes() == b"keep me"


def test_schema9_hevc_job_stays_compatible(tmp_path, media_tools):
    source = clip(tmp_path / "older.mp4")
    preview = plan([source], "mkv", "hevc")
    specs = video_snapshot(preview)
    specs[0]["tools"].pop("opus")
    assert "channel_layout" not in specs[0]["video"]["streams"][1]
    store = Store(tmp_path / "history")
    job = store.enqueue("Older HEVC", "video", specs)
    store.db.execute("PRAGMA user_version=9")
    store.close()
    store = Store(tmp_path / "history")
    try:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA
        assert execute_job(store, job) == "succeeded", store.items(job)
    finally:
        store.close()


def test_webm_alpha_metadata_blocks_transparency_loss(tmp_path, media_tools):
    source = tmp_path / "transparent.webm"
    command([media_tools["ffmpeg"], "-v", "error", "-f", "lavfi", "-i", "color=red@0.5:size=64x64:rate=12,format=yuva420p",
        "-t", "0.5", "-c:v", "libvpx-vp9", "-threads", "1", "-pix_fmt", "yuva420p", str(source)])
    before = source.read_bytes()
    assert probe(source, media_tools)["streams"][0]["alpha"]
    preview = webm([FileRecord.from_path(source)])
    assert preview.counts == {"Invalid": 1}
    assert "alpha video are blocked" in preview.items[0].reason
    assert source.read_bytes() == before
