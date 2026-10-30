import json
import array
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from media_tool.jobs import Store, execute_job
from media_tool.video import video_snapshot, encode_command, probe
from test_foundation import app, window, wait_until
from test_video import media_tools, clip, run_plan
from test_video_formats import plan


def target(records, container="mp4", **options):
    return plan(records, container, "hevc", video_mode="target_size", video_target_size=.10, **options)


@pytest.mark.parametrize("container,audio,preset", [("mp4",0,"superfast"), ("mkv",1,"veryfast"), ("mp4",1,"medium")])
def test_hevc_two_pass_budget_and_owned_logs(tmp_path, media_tools, container, audio, preset):
    source = clip(tmp_path / "source 日本.mp4", audio=audio, duration=2)
    before = Path(source.path).read_bytes()
    preview = target([source], container, video_preset=preset)
    assert preview.counts == {"Planned": 1}, preview.items
    args = encode_command(preview.items[0].video_spec, tmp_path / "output.part", 1, tmp_path / "unused", 200000)
    assert "-pass" not in args and "pass=1:stats=x265.log:slow-firstpass=1" in args[args.index("-x265-params")+1]
    item = run_plan(tmp_path, preview)[0]
    assert item["state"] == "succeeded", dict(item)
    assert Path(preview.items[0].output).stat().st_size <= 100000
    assert probe(preview.items[0].output, media_tools)["streams"][0]["codec_name"] == "hevc"
    assert Path(source.path).read_bytes() == before
    assert not list((tmp_path / "history").glob(".mediatool-pass-*"))


@pytest.mark.parametrize("codec", ["hevc", "h264"])
def test_completed_analysis_is_reused_after_cancelled_output_pass(tmp_path, media_tools, codec):
    source = clip(tmp_path / "source.mp4", audio=0, duration=2)
    preview = plan([source], "mp4", codec, video_mode="target_size", video_target_size=.1, video_reuse_analysis=True)
    store = Store(tmp_path / "history")
    cancelled = threading.Event()
    points = []
    try:
        job = store.enqueue("Target", "video", video_snapshot(preview))
        execute_job(store, job, cancelled.is_set, hook=lambda point: cancelled.set() if point == "pass2_started" else None)
        item = store.items(job)[0]
        assert item["state"] == "cancelled", dict(item)
        assert json.loads(item["stage"])["analysis"]["logs"]
        assert not Path(preview.items[0].output).exists()
        store.retry(job)
        assert execute_job(store, job, hook=points.append) == "succeeded", store.items(job)
        assert "analysis_reused" in points and "pass1_started" not in points
        assert not store.items(job)[0]["stage"]
        assert not list(store.directory.glob(".mediatool-pass-*"))
    finally:
        store.close()


def test_hevc_target_enabled_and_checkpoint_choice_frozen(window):
    window._tool_changed("Video")
    codec = window.video_format_controls.codec
    codec.setCurrentIndex(codec.findData("hevc"))
    controls = window.compression_controls
    assert controls.mode.model().item(controls.mode.findData("target_size")).isEnabled()
    controls.mode.setCurrentIndex(controls.mode.findData("target_size"))
    controls.reuse.setChecked(True)
    assert window.plan_request().video_reuse_analysis
    assert window.plan_request().video_mode == "target_size"
    window.format.setCurrentText("WebM")
    assert controls.mode.currentData() == "quality"


@pytest.mark.parametrize("always_miss", [False, True])
def test_hevc_cap_allows_only_one_complete_retry(tmp_path, media_tools, monkeypatch, always_miss):
    import media_tool.video as video
    source = clip(tmp_path / "source.mp4", audio=0)
    preview = target([source])
    real, passes = video.run_process, []
    def padded(args, *positional, **kwargs):
        result = real(args, *positional, **kwargs)
        if "-x265-params" in args:
            params = args[args.index("-x265-params") + 1]
            number = int(next(p.split("=")[1] for p in params.split(":") if p.startswith("pass=")))
            passes.append(number)
            if number == 2 and (always_miss or len(passes) == 2):
                with Path(args[-1]).open("ab") as handle:
                    handle.write(b"\0" * 150000)
        return result
    monkeypatch.setattr(video, "run_process", padded)
    item = run_plan(tmp_path, preview)[0]
    assert passes == [1, 2, 1, 2]
    assert item["state"] == ("failed" if always_miss else "succeeded"), dict(item)
    if always_miss:
        assert "Target missed" in item["message"] and not Path(preview.items[0].output).exists()
    else:
        assert Path(preview.items[0].output).stat().st_size <= 100000
    assert not list((tmp_path / "history").glob(".mediatool-pass-*"))


@pytest.mark.parametrize("point", ["analysis_saved", "pass2_started"])
def test_hevc_completed_analysis_survives_process_loss(tmp_path, media_tools, point):
    source = clip(tmp_path / "source.mp4", audio=0)
    preview = target([source], video_reuse_analysis=True)
    store = Store(tmp_path / "history")
    try:
        job = store.enqueue("Target", "video", video_snapshot(preview))
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
            timeout=40, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        assert result.returncode == 73, result.stderr.decode(errors="replace")
        assert store.recover() == 1 and store.retry(job) == 1
        points = []
        assert execute_job(store, job, hook=points.append) == "succeeded", store.items(job)
        assert "analysis_reused" in points and "pass1_started" not in points
        assert Path(preview.items[0].output).stat().st_size <= 100000
        assert Path(source.path).exists() and not store.items(job)[0]["stage"]
    finally:
        store.close()


def test_hevc_target_preserves_measured_flash_tone_alignment(tmp_path, media_tools):
    source = clip(tmp_path / "sync.mp4", sync=True)
    preview = target([source])
    item = run_plan(tmp_path, preview)[0]
    assert item["state"] == "succeeded", dict(item)
    offsets = []
    for path in (source.path, preview.items[0].output):
        common = [media_tools["ffmpeg"], "-v", "error", "-i", str(path)]
        flags = dict(creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        frames = subprocess.check_output(common + ["-map", "0:v:0", "-vf", "scale=1:1,format=gray", "-f", "rawvideo", "-"], **flags)
        raw = subprocess.check_output(common + ["-map", "0:a:0", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"], **flags)
        samples = array.array("h", raw)
        visual = next(i for i, value in enumerate(frames) if value > 128) / 12
        audible = next(i for i, value in enumerate(samples) if abs(value) > 2000) / 16000
        offsets.append(audible - visual)
    assert abs(offsets[1] - offsets[0]) < 0.1
