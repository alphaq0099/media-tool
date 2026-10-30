import array
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from media_tool.domain import FileRecord
from media_tool.jobs import Store, execute_job
from media_tool.planning import PlanRequest, PlanCancelled
from media_tool.processes import run_process
from media_tool.video import (inspect_tools, plan_video, video_snapshot, probe, select_streams, verify_output)
from test_foundation import app, window, wait_until


@pytest.fixture(scope="module")
def media_tools():
    try:
        return inspect_tools()
    except ValueError as error:
        pytest.skip(str(error))


def clip(path, audio=1, duration=1, size="160x90", sync=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    video = "color=black:size=160x90:rate=12" if sync else f"testsrc2=size={size}:rate=12"
    args = ["ffmpeg", "-hide_banner", "-v", "error", "-f", "lavfi", "-i", video]
    for index in range(audio):
        tone = ("aevalsrc=if(gte(t\\,0.5)\\,sin(2*PI*600*t)\\,0):s=48000" if sync else
                f"sine=frequency={440*(index+1)}:sample_rate=48000")
        args += ["-f", "lavfi", "-i", tone]
    args += ["-map", "0:v:0"]
    for index in range(audio):
        args += ["-map", f"{index+1}:a:0"]
    if sync:
        args += ["-vf", "drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='gte(t,0.5)'"]
    webm = path.suffix == ".webm"
    args += ["-t", str(duration), "-c:v", "libvpx-vp9" if webm else "libx264", "-threads", "1", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "libopus" if webm else "aac"]
    args += [str(path)]
    subprocess.run(args, check=True, capture_output=True, timeout=30,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return FileRecord.from_path(path)


def make_plan(records, **options):
    return plan_video(records, PlanRequest("video", extension="mp4", **options))


def run_plan(tmp_path, plan, **options):
    store = Store(tmp_path / "history")
    try:
        job = store.enqueue("Video", "video", video_snapshot(plan))
        execute_job(store, job, **options)
        return store.items(job)
    finally:
        store.close()


@pytest.mark.parametrize("extension", ["mp4", "mkv", "mov", "webm"])
@pytest.mark.parametrize("audio", [0, 1])
def test_real_conversion_formats_and_silent_input_keep_original(tmp_path, media_tools, extension, audio):
    source = clip(tmp_path / f"clip 日本 ' [1].{extension}", audio=audio)
    before = Path(source.path).read_bytes()
    preview = make_plan([source])
    assert preview.counts == {"Planned": 1}, preview.items
    items = run_plan(tmp_path, preview)
    assert items[0]["state"] == "succeeded", items
    output = probe(preview.items[0].output, media_tools)
    assert output["streams"][0]["codec_name"] == "h264"
    assert len([s for s in output["streams"] if s["codec_type"] == "audio"]) == audio
    assert Path(source.path).read_bytes() == before
    assert Path(source.path).stat().st_mtime_ns == source.modified_ns
    assert not list(tmp_path.glob("*.part"))


def test_extra_tracks_require_explicit_review_and_selected_audio_is_used(tmp_path, media_tools):
    source = clip(tmp_path / "multiple.mkv", audio=2)
    preview = make_plan([source], video_audio=2)
    assert preview.counts == {"Review": 1}
    assert "Omit: #1 audio" in preview.items[0].reason
    with pytest.raises(ValueError, match="omitted tracks"):
        video_snapshot(preview)
    preview = make_plan([source], video_audio=2, video_allow_omissions=True)
    items = run_plan(tmp_path, preview)
    assert items[0]["state"] == "succeeded", items
    raw = subprocess.check_output([media_tools["ffmpeg"], "-v", "error", "-i", preview.items[0].output,
        "-map", "0:a:0", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    samples = array.array("h", raw)[4000:12000]
    crossings = sum(a < 0 <= b for a,b in zip(samples, samples[1:]))
    assert 850 < crossings * 2 < 910  # second source tone is 880 Hz


def test_subtitle_omission_is_listed_before_conversion(tmp_path, media_tools):
    source = clip(tmp_path / "source.mp4")
    subtitle = tmp_path / "captions.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:00,900\nFixture subtitle\n", encoding="utf-8")
    target = tmp_path / "subtitles.mkv"
    subprocess.run([media_tools["ffmpeg"], "-v", "error", "-i", source.path, "-i", str(subtitle),
        "-map", "0", "-map", "1:0", "-c", "copy", str(target)], check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    preview = make_plan([FileRecord.from_path(target)])
    assert preview.counts == {"Review": 1}
    assert "subtitle subrip" in preview.items[0].reason
    allowed = make_plan([FileRecord.from_path(target)], video_allow_omissions=True)
    assert run_plan(tmp_path, allowed)[0]["state"] == "succeeded"


def test_missing_audio_track_and_corrupt_input_are_actionable(tmp_path, media_tools):
    source = clip(tmp_path / "source.mp4")
    preview = make_plan([source], video_audio=2)
    assert preview.counts == {"Invalid": 1}
    assert "Audio track 2 is unavailable" in preview.items[0].reason
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video")
    assert make_plan([FileRecord.from_path(broken)]).counts == {"Invalid": 1}


@pytest.mark.parametrize("change", [
    {"pix_fmt": "yuv420p10le"}, {"color_transfer": "smpte2084"}, {"color_primaries": "bt2020"},
    {"rotation": 45}, {"width": 1}, {"sample_aspect_ratio": "-1:3"}, {"field_order": "tt"},
])
def test_unsupported_color_and_geometry_are_explicitly_blocked(tmp_path, media_tools, change):
    source = clip(tmp_path / "source.mp4", audio=0)
    info = probe(source.path, media_tools)
    info["streams"][0].update(change)
    with pytest.raises(ValueError):
        select_streams(info, 1)


def test_unknown_duration_uses_indeterminate_progress(tmp_path, media_tools, monkeypatch):
    import media_tool.video as video
    source = clip(tmp_path / "source.mp4", audio=0)
    original = video.probe
    def unknown(*args, **kwargs):
        info = original(*args, **kwargs)
        info["duration"] = None
        for stream in info["streams"]:
            stream["duration"] = None
        return info
    monkeypatch.setattr(video, "probe", unknown)
    preview = make_plan([source])
    assert "Duration unknown" in preview.items[0].reason
    seen = []
    items = run_plan(tmp_path, preview, progress=lambda item, done, total: seen.append(total))
    assert items[0]["state"] == "succeeded", items[0]["message"]
    assert 0 in seen


@pytest.mark.parametrize('durations', [True, False])
def test_packet_span_handles_reordered_timestamps_without_guessing_last_frame(monkeypatch, durations):
    import media_tool.video as video
    def packets(*args, on_line, **kwargs):
        for pts in (0, .3, .1, .2):
            on_line(f'stream_index=0|pts_time={pts}|duration_time={"0.1" if durations else "N/A"}')
        return ''
    monkeypatch.setattr(video, 'run_process', packets)
    result=video.packet_durations('synthetic.mp4', {'streams':[{'index':0,'codec_type':'video','duration':None}]}, {'ffprobe':'ffprobe'}, lambda:False)
    assert result['streams'][0]['duration']==pytest.approx(.4 if durations else .3)
    assert result['streams'][0]['duration_basis']==('packet_end' if durations else 'presentation_span_lower_bound')


@pytest.mark.parametrize('diagnostic', ['', 'File ended prematurely'])
def test_missing_packet_duration_does_not_bypass_unverifiable_or_truncated_input(monkeypatch, diagnostic):
    import media_tool.video as video
    def packets(*args, on_line, **kwargs):
        on_line('stream_index=0|pts_time=0|duration_time=N/A')
        if diagnostic:on_line('stream_index=0|pts_time=1|duration_time=N/A')
        return diagnostic
    monkeypatch.setattr(video, 'run_process', packets)
    with pytest.raises(ValueError):
        video.packet_durations('synthetic.mp4', {'streams':[{'index':0,'codec_type':'video','duration':None}]}, {'ffprobe':'ffprobe'}, lambda:False)


@pytest.mark.parametrize("point", ["created", "progress", "written", "prepared"])
def test_cancel_and_retry_preserve_source_and_remove_partials(tmp_path, media_tools, point):
    source = clip(tmp_path / "source.mp4", audio=0)
    preview = make_plan([source])
    before = Path(source.path).read_bytes()
    store = Store(tmp_path / "history")
    cancelled = threading.Event()
    try:
        job = store.enqueue("Video", "video", video_snapshot(preview))
        execute_job(store, job, cancelled.is_set, hook=lambda stage: cancelled.set() if stage == point else None)
        assert store.items(job)[0]["state"] == "cancelled", store.items(job)
        assert not Path(preview.items[0].output).exists()
        assert not list(tmp_path.glob("*.part"))
        store.retry(job)
        assert execute_job(store, job) == "succeeded", store.items(job)
        assert Path(source.path).read_bytes() == before
    finally:
        store.close()


def test_late_collision_and_modified_original_never_commit(tmp_path, media_tools):
    source = clip(tmp_path / "source.mp4", audio=0)
    preview = make_plan([source])
    output = Path(preview.items[0].output)
    items = run_plan(tmp_path, preview, hook=lambda point: output.write_bytes(b"unrelated") if point == "prepared" else None)
    assert items[0]["state"] == "failed"
    assert output.read_bytes() == b"unrelated"
    assert not list(tmp_path.glob("*.part"))


def test_output_verification_rejects_wrong_codec_duration_and_offsets(tmp_path, media_tools):
    source = clip(tmp_path / "source.mp4")
    preview = make_plan([source])
    spec = preview.items[0].video_spec
    info = copy.deepcopy(spec["video"])
    verify_output(spec, info)
    info["streams"][0]["codec_name"] = "hevc"
    with pytest.raises(ValueError, match="codec"):
        verify_output(spec, info)
    info = copy.deepcopy(spec["video"])
    info["streams"][0]["duration"] += 10
    with pytest.raises(ValueError, match="duration"):
        verify_output(spec, info)
    info = copy.deepcopy(spec["video"])
    info["streams"][1]["start"] += 1
    with pytest.raises(ValueError, match="offset"):
        verify_output(spec, info)


def test_process_limits_timeout_and_cancellation_leave_no_child(tmp_path):
    with pytest.raises(TimeoutError):
        run_process([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.3)
    with pytest.raises(ValueError, match="inspection limit"):
        run_process([sys.executable, "-c", "print('x'*20000)"], output_limit=100)
    with pytest.raises(Exception, match="Cancelled"):
        run_process([sys.executable, "-c", "print('never')"], cancelled=lambda: True)


def test_missing_tools_and_encoder_are_actionable(monkeypatch):
    import media_tool.video as video
    monkeypatch.setattr(video, "resolve_tool", lambda name: None)
    with pytest.raises(ValueError, match="FFmpeg and FFprobe"):
        video.inspect_tools()


def test_ui_direct_video_conversion_does_not_drain_waiting_jobs(window, tmp_path, media_tools):
    source = clip(tmp_path / "source.mp4", audio=0)
    window._tool_changed("Video")
    assert window.plan_request().video_crf == 23
    assert window.plan_request().video_preset == "veryfast"
    window.detail.setCurrentText("Best")
    window.video_speed.setCurrentText("Fastest")
    assert window.plan_request().video_crf == 18
    assert window.plan_request().video_preset == "superfast"
    window.start_scan([source.path])
    wait_until(lambda: not window.busy)
    queue = window.ensure_jobs()
    waiting = queue.store.enqueue("Waiting", "sample", [{"name": "later", "seconds": 0}])
    window.start_rename_button.click()
    wait_until(lambda: not window.busy and not queue.enabled, timeout=20000)
    assert window.result_model.items[0]["state"] == "succeeded", window.status.text()
    assert not queue.isVisible()
    assert queue.store.items(waiting)[0]["attempts"] == 0
    assert window.model.rowCount() == 0
    assert not window.result_can_undo
    assert Path(source.path).exists()


def test_ui_omissions_block_direct_run_and_show_details(window, tmp_path, media_tools, monkeypatch):
    source = clip(tmp_path / "multiple.mkv", audio=2)
    window._tool_changed("Video")
    window.video_audio_mode.setCurrentIndex(window.video_audio_mode.findData("custom"))
    window.video_audio_tracks.setText("1")
    window.model.append([source])
    window.start_rename_button.click()
    wait_until(lambda: not window.busy, timeout=15000)
    assert window.last_plan.counts == {"Review": 1}
    assert not window.jobs_dialog.store.jobs()
    shown = []
    monkeypatch.setattr(window, "_show_text", lambda title, body: shown.append(body))
    window.stream_details_button.click()
    assert "Omit:" in shown[0] and "#2 audio" in shown[0]
    window.video_omissions.setChecked(True)
    window.start_rename_button.click()
    wait_until(lambda: not window.busy and not window.jobs_dialog.enabled, timeout=20000)
    assert window.result_model.items[0]["state"] == "succeeded", window.status.text()


@pytest.mark.parametrize("point", ["created", "progress", "written", "prepared", "committed"])
def test_real_process_loss_and_saved_recovery(tmp_path, media_tools, point):
    source = clip(tmp_path / "original.mp4", audio=0)
    before = Path(source.path).read_bytes()
    preview = make_plan([source])
    store = Store(tmp_path / "history")
    try:
        job = store.enqueue("Video", "video", video_snapshot(preview))
        code = """import os,sys
from media_tool.jobs import Store,execute_job
store=Store(sys.argv[1])
def crash(point):
    if point == sys.argv[3]: os._exit(73)
execute_job(store,sys.argv[2],hook=crash)
"""
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        result = subprocess.run([sys.executable, "-c", code, str(store.directory), job, point],
            env=env, timeout=20, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        assert result.returncode == 73, result.stderr.decode(errors="replace")
        time.sleep(0.15)  # allow EOF supervision to retire a child active at the crash boundary
        assert store.recover() == 1
        assert store.retry(job) == 1
        assert execute_job(store, job) == "succeeded", store.items(job)
        assert Path(source.path).read_bytes() == before
        assert not list(tmp_path.glob("*.part"))
        if point == "committed":
            assert "Recovered" in store.items(job)[0]["message"]
    finally:
        store.close()


def test_parent_process_loss_stops_supervised_child(tmp_path):
    import psutil
    pidfile = tmp_path / "child.pid"
    child_code = "import os,sys,time; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)"
    parent_code = """import sys
from media_tool.processes import run_process
run_process([sys.executable,'-c',sys.argv[2],sys.argv[1]])
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    parent = subprocess.Popen([sys.executable, "-c", parent_code, str(pidfile), child_code], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    child = None
    def child_alive():
        try:
            return child is not None and child.is_running() and child.status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False
    try:
        deadline = time.monotonic() + 8
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pidfile.exists()
        child = psutil.Process(int(pidfile.read_text()))
        parent.kill()
        parent.wait(timeout=5)
        deadline = time.monotonic() + 5
        while child_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not child_alive()
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait()
        if child_alive():
            child.kill()


def test_generated_flash_and_tone_keep_av_alignment(tmp_path, media_tools):
    source = clip(tmp_path / "sync.mp4", sync=True)
    preview = make_plan([source])
    items = run_plan(tmp_path, preview)
    assert items[0]["state"] == "succeeded", items
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


def test_unavailable_encoder_and_source_change_fail_safely(tmp_path, media_tools, monkeypatch):
    import media_tool.video as video
    source = clip(tmp_path / "source.mp4", audio=0)
    preview = make_plan([source])
    Path(source.path).write_bytes(b"changed after preview")
    assert run_plan(tmp_path, preview)[0]["state"] == "failed"
    assert not Path(preview.items[0].output).exists()
    monkeypatch.setattr(video, "run_process", lambda *args, **kwargs: " V..... different_encoder\n")
    with pytest.raises(ValueError, match="libx264"):
        video.inspect_tools()


def test_truncated_source_cannot_pass_complete_conversion(tmp_path, media_tools):
    source = clip(tmp_path / "truncated.mkv", audio=0, duration=3)
    path = Path(source.path)
    path.write_bytes(path.read_bytes()[:len(path.read_bytes()) // 2])
    before = path.read_bytes()
    preview = make_plan([FileRecord.from_path(path)])
    if preview.counts == {"Planned": 1}:
        assert run_plan(tmp_path, preview)[0]["state"] == "failed"
        assert not Path(preview.items[0].output).exists()
    else:
        assert preview.counts == {"Invalid": 1}
    assert path.read_bytes() == before


def test_actual_ten_bit_input_is_blocked(tmp_path, media_tools):
    target = tmp_path / "ten-bit.mp4"
    subprocess.run([media_tools["ffmpeg"], "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=12",
        "-t", "0.5", "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p10le", str(target)],
        check=True, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    preview = make_plan([FileRecord.from_path(target)])
    assert preview.counts == {"Invalid": 1}
    assert "8-bit SDR" in preview.items[0].reason


def test_output_open_uses_selected_successful_video(window, tmp_path, monkeypatch):
    from PySide6.QtGui import QDesktopServices
    output = tmp_path / "converted.mp4"
    output.write_bytes(b"fixture path only")
    window.result_is_video = True
    window.result_model.set_items((dict(source="unused", output=str(output), state="succeeded", details=""),))
    window.results_table.selectRow(0)
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: (opened.append(url.toLocalFile()), True)[1])
    window.open_video_output()
    assert [Path(path) for path in opened] == [output]


@pytest.mark.parametrize("crf,preset,percent", [(20, "superfast", 100), (20, "medium", 100),
    (23, "veryfast", 100), (18, "superfast", 100), (20, "superfast", 50)])
def test_variable_frame_timing_preserves_frames_and_timestamp_precision(tmp_path, media_tools, crf, preset, percent):
    source = tmp_path / "screen recording.mp4"
    # 60-fps bursts, then 20 fps, with a nominal 30-fps codec hint like capture files.
    # The former output clock collapses adjacent timestamps and fails MP4 muxing.
    subprocess.run([media_tools["ffmpeg"], "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=60",
        "-vf", "select='if(lt(t,0.5),1,not(mod(n,3)))'", "-t", "2", "-fps_mode:v", "passthrough",
        "-enc_time_base:v", "1:30000", "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1",
        "-pix_fmt", "yuv420p", "-bsf:v", "h264_metadata=tick_rate=60:fixed_frame_rate_flag=0", str(source)],
        check=True, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    original = source.read_bytes()
    preview = make_plan([FileRecord.from_path(source)], video_crf=crf, video_preset=preset,
                        resize_mode="percent" if percent != 100 else "none", resize_percent=percent)
    items = run_plan(tmp_path, preview)
    assert items[0]["state"] == "succeeded", items
    packet_sets = []
    for path in (source, Path(preview.items[0].output)):
        data = json.loads(subprocess.check_output([media_tools["ffprobe"], "-v", "error", "-select_streams", "v:0",
            "-show_packets", "-show_entries", "packet=pts_time,dts_time", "-of", "json", str(path)],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))
        pts = sorted(float(packet["pts_time"]) for packet in data["packets"])
        dts = [float(packet["dts_time"]) for packet in data["packets"]]
        assert all(b > a for a,b in zip(dts,dts[1:]))
        packet_sets.append(pts)
    before, after = packet_sets
    assert len(before) == len(after) > 30
    steps = [b-a for a,b in zip(before,before[1:])]
    assert max(steps) > 2 * min(steps)
    assert max(abs((a-before[0])-(b-after[0])) for a,b in zip(before,after)) < 0.0001
    assert source.read_bytes() == original
