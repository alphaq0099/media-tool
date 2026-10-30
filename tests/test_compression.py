import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from media_tool.compression import target_bytes, size_budget
from media_tool.jobs import SCHEMA, Store, execute_job
from media_tool.video import video_snapshot
from media_tool.video_stats import create_scope, cleanup_scope
from test_foundation import app, window, wait_until
from test_video import media_tools, clip, make_plan, run_plan


def budget_info(duration=10, audio=True):
    video = dict(duration=duration, start=0, avg_frame_rate="30/1")
    sound = dict(duration=duration, start=0) if audio else None
    return dict(duration=duration), video, sound


@pytest.mark.parametrize("amount,unit,expected", [(1, "MB", 1_000_000), (1, "MiB", 1_048_576),
    (0.1, "MB", 100_000), ("2.35", "MiB", 2_464_153)])
def test_explicit_byte_units(amount, unit, expected):
    assert target_bytes(amount, unit) == expected


@pytest.mark.parametrize("amount,unit", [(0, "MB"), (-1, "MiB"), ("nan", "MB"), ("inf", "MB"),
    (1, "GB"), (None, "MB"), (1_000_001, "MB")])
def test_invalid_targets_are_rejected(amount, unit):
    with pytest.raises(ValueError):
        target_bytes(amount, unit)


@pytest.mark.parametrize("duration,audio", [(0.1, False), (1, True), (30, True), (600, False)])
def test_budget_includes_audio_and_overhead(duration, audio):
    info, video, sound = budget_info(duration, audio)
    budget = size_budget(20, "MB", 128, info, video, sound)
    assert budget["overhead_bytes"] >= 16_384
    assert budget["audio_bitrate"] == (128_000 if audio else 0)
    assert budget["video_bitrate"] * duration / 8 + budget["audio_bytes"] + budget["overhead_bytes"] <= budget["target_bytes"]


def test_impossible_budget_and_unknown_duration_are_actionable():
    info, video, audio = budget_info(3600)
    with pytest.raises(ValueError, match="too small"):
        size_budget(1, "MB", 192, info, video, audio)
    info["duration"] = video["duration"] = audio["duration"] = None
    with pytest.raises(ValueError, match="known duration"):
        size_budget(10, "MB", 128, info, video, audio)
    info["duration"] = video["duration"] = 10
    assert size_budget(10, "MB", 128, info, video, audio)["container_duration_used"]
    info["duration"] = None
    with pytest.raises(ValueError, match="selected video/audio"):
        size_budget(10, "MB", 128, info, video, audio)


@pytest.mark.parametrize("audio,preset", [(0, "superfast"), (1, "veryfast"), (1, "medium")])
def test_real_two_pass_output_is_below_cap_and_keeps_original(tmp_path, media_tools, audio, preset):
    source = clip(tmp_path / "source 日本.mp4", audio=audio, duration=2, sync=preset == "medium")
    before = Path(source.path).read_bytes()
    plan = make_plan([source], video_mode="target_size", video_target_size=0.10, video_preset=preset)
    assert plan.counts == {"Planned": 1}, plan.items
    assert "Maximum 0.1 MB per file" in plan.items[0].reason
    items = run_plan(tmp_path, plan)
    assert items[0]["state"] == "succeeded", items
    assert "Target met:" in items[0]["message"]
    assert Path(plan.items[0].output).stat().st_size <= 100_000
    assert Path(source.path).read_bytes() == before
    assert not items[0]["stage"]
    assert not list((tmp_path / "history").glob(".mediatool-pass-*"))


def test_very_large_cap_does_not_require_padding_to_fill_it(tmp_path, media_tools):
    source = clip(tmp_path / "short.mp4", audio=0)
    plan = make_plan([source], video_mode="target_size", video_target_size=10)
    result = run_plan(tmp_path, plan)
    assert result[0]["state"] == "succeeded", result
    assert Path(plan.items[0].output).stat().st_size <= 10_000_000


@pytest.mark.parametrize("point", ["pass1_started", "pass1_finished", "pass2_started", "pass2_finished"])
def test_cancel_either_pass_and_retry_cleans_owned_stats(tmp_path, media_tools, point):
    source = clip(tmp_path / "source.mp4", audio=0)
    before = Path(source.path).read_bytes()
    plan = make_plan([source], video_mode="target_size", video_target_size=0.1)
    store = Store(tmp_path / "history")
    cancel = threading.Event()
    try:
        job = store.enqueue("Target", "video", video_snapshot(plan))
        execute_job(store, job, cancelled=cancel.is_set, hook=lambda stage: cancel.set() if stage == point else None)
        item = store.items(job)[0]
        assert item["state"] == "cancelled", item
        assert not item["stage"]
        assert not list((tmp_path / "history").glob(".mediatool-pass-*"))
        assert not Path(plan.items[0].output).exists()
        store.retry(job)
        assert execute_job(store, job) == "succeeded"
        assert Path(plan.items[0].output).stat().st_size <= 100_000
        assert Path(source.path).read_bytes() == before
    finally:
        store.close()


@pytest.mark.parametrize("always_miss", [False, True])
def test_only_one_automatic_retry_and_no_oversize_success(tmp_path, media_tools, monkeypatch, always_miss):
    import media_tool.video as video
    source = clip(tmp_path / "source.mp4", audio=0)
    plan = make_plan([source], video_mode="target_size", video_target_size=0.10)
    real = video.run_process
    passes = []
    def padded(args, *positional, **kwargs):
        result = real(args, *positional, **kwargs)
        if "-pass" in args:
            number = int(args[args.index("-pass") + 1])
            passes.append(number)
            if number == 2 and (always_miss or len(passes) == 2):
                with Path(args[-1]).open("ab") as output:
                    output.write(b"\0" * 150_000)
        return result
    monkeypatch.setattr(video, "run_process", padded)
    result = run_plan(tmp_path, plan)
    assert passes == [1, 2, 1, 2]
    if always_miss:
        assert result[0]["state"] == "failed"
        assert "Target missed:" in result[0]["message"]
        assert not Path(plan.items[0].output).exists()
    else:
        assert result[0]["state"] == "succeeded", result
        assert Path(plan.items[0].output).stat().st_size <= 100_000
    assert not list((tmp_path / "history").glob(".mediatool-pass-*"))
    assert not list(tmp_path.glob("*.part"))


def test_unrelated_stats_file_is_preserved(tmp_path):
    store = Store(tmp_path / "history")
    try:
        job = store.enqueue("Sample", "sample", [{"name": "one", "seconds": 0}])
        item = store.items(job)[0]
        scope = create_scope(store, item["id"])
        root = Path(scope["path"])
        alien = root / "user-file.txt"
        alien.write_text("keep me")
        with pytest.raises(ValueError, match="unrelated"):
            cleanup_scope(store, item["id"])
        assert alien.read_text() == "keep me"
        alien.unlink()
        cleanup_scope(store, item["id"])
        assert not root.exists()
    finally:
        store.close()


def test_replaced_stats_file_is_preserved(tmp_path):
    store = Store(tmp_path / "history")
    try:
        job = store.enqueue("Sample", "sample", [{"name": "one", "seconds": 0}])
        item = store.items(job)[0]
        scope = create_scope(store, item["id"])
        root = Path(scope["path"])
        log = root / "stats-0.log"
        log.rename(tmp_path / "saved-own-log")
        log.write_text("unrelated replacement")
        with pytest.raises(ValueError, match="replaced"):
            cleanup_scope(store, item["id"])
        assert log.read_text() == "unrelated replacement"
        log.unlink()
        cleanup_scope(store, item["id"])
    finally:
        store.close()


def test_target_ui_runs_directly_with_frozen_units_and_audio_budget(window, tmp_path, media_tools):
    source = clip(tmp_path / "input.mp4", audio=1)
    window._tool_changed("Video")
    controls = window.compression_controls
    assert controls.mode.currentData() == "quality"
    controls.mode.setCurrentIndex(controls.mode.findData("target_size"))
    controls.amount.setValue(0.10)
    controls.unit.setCurrentText("MiB")
    controls.audio.setCurrentIndex(controls.audio.findData(96))
    assert not window.detail.isVisible()
    assert window.start_rename_button.text() == "Start Compress"
    window.model.append([source])
    window.start_rename_button.click()
    wait_until(lambda: not window.busy and not window.jobs_dialog.enabled, timeout=30000)
    assert window.result_model.items[0]["state"] == "succeeded", window.status.text()
    jobs = window.jobs_dialog.store.jobs()
    spec = json.loads(window.jobs_dialog.store.items(jobs[-1]["id"])[0]["spec"])
    assert spec["budget"]["target_bytes"] == 104_857
    assert spec["budget"]["audio_bitrate"] == 96_000
    assert Path(window.result_model.items[0]["output"]).stat().st_size <= 104_857
    controls.mode.setCurrentIndex(0)
    assert window.detail.isVisible()


def test_tight_resolution_advice_never_changes_user_dimensions(tmp_path, media_tools):
    source = clip(tmp_path / "clip.mp4", audio=0, duration=2, size="640x360")
    plan = make_plan([source], video_mode="target_size", video_target_size=0.04)
    assert plan.counts == {"Planned": 1}, plan.items
    assert "consider resizing" in plan.items[0].reason
    assert plan.items[0].video_spec["output_size"] == [640, 360]


def test_impossible_target_blocks_ui_before_encoding(window, tmp_path, media_tools):
    source = clip(tmp_path / "clip.mp4")
    before = Path(source.path).read_bytes()
    window._tool_changed("Video")
    controls = window.compression_controls
    controls.mode.setCurrentIndex(controls.mode.findData("target_size"))
    controls.amount.setValue(0.01)
    window.model.append([source])
    window.start_rename_button.click()
    wait_until(lambda: not window.busy, timeout=15000)
    assert window.last_plan.counts == {"Invalid": 1}
    assert "too small" in window.last_plan.items[0].reason
    assert not window.jobs_dialog.store.jobs()
    assert Path(source.path).read_bytes() == before
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize("point", ["pass1_finished", "pass2_started", "committed"])
def test_process_loss_reconciles_stats_and_preserves_cap(tmp_path, media_tools, point):
    source = clip(tmp_path / "source.mp4", audio=0)
    before = Path(source.path).read_bytes()
    plan = make_plan([source], video_mode="target_size", video_target_size=0.10)
    store = Store(tmp_path / "history")
    try:
        job = store.enqueue("Target", "video", video_snapshot(plan))
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
        assert not list(store.directory.glob(".mediatool-pass-*"))
        assert Path(plan.items[0].output).stat().st_size <= 100_000
        assert Path(source.path).read_bytes() == before
    finally:
        store.close()


@pytest.mark.parametrize("codec", ["h264", "hevc"])
def test_two_pass_resize_preserves_variable_frame_timestamps(tmp_path, media_tools, codec):
    from media_tool.domain import FileRecord
    source = tmp_path / "vfr.mp4"
    subprocess.run([media_tools["ffmpeg"], "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=60",
        "-vf", "select='if(lt(t,0.5),1,not(mod(n,3)))'", "-t", "2", "-fps_mode:v", "passthrough",
        "-enc_time_base:v", "1:30000", "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1",
        "-pix_fmt", "yuv420p", "-bsf:v", "h264_metadata=tick_rate=60:fixed_frame_rate_flag=0", str(source)],
        check=True, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    plan = make_plan([FileRecord.from_path(source)], video_mode="target_size", video_target_size=0.1,
                     resize_mode="percent", resize_percent=50, video_codec=codec)
    result = run_plan(tmp_path, plan)
    assert result[0]["state"] == "succeeded", result
    pts = []
    for path in (source, Path(plan.items[0].output)):
        data = json.loads(subprocess.check_output([media_tools["ffprobe"], "-v", "error", "-select_streams", "v:0",
            "-show_packets", "-show_entries", "packet=pts_time", "-of", "json", str(path)],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))
        pts.append(sorted(float(p["pts_time"]) for p in data["packets"]))
    assert len(pts[0]) == len(pts[1]) > 30
    assert max(abs((a-pts[0][0])-(b-pts[1][0])) for a,b in zip(*pts)) < 0.0001
    assert Path(plan.items[0].output).stat().st_size <= 100_000


def test_schema_seven_quality_job_remains_quality_based(tmp_path, media_tools):
    source = clip(tmp_path / "older.mp4", audio=0)
    plan = make_plan([source])
    assert "budget" not in video_snapshot(plan)[0]
    store = Store(tmp_path / "history")
    job = store.enqueue("Older quality job", "video", video_snapshot(plan))
    store.db.execute("PRAGMA user_version=7")
    store.close()
    store = Store(tmp_path / "history")
    try:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA
        assert execute_job(store, job) == "succeeded"
        assert "Target met" not in store.items(job)[0]["message"]
    finally:
        store.close()
