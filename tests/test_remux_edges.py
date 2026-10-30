import array
import os
from pathlib import Path
import subprocess
import sys

import pytest

from media_tool.domain import FileRecord
from media_tool.jobs import Store, execute_job
from media_tool.video import probe, video_snapshot
from test_video import media_tools, clip, run_plan
from test_video_formats import plan
from test_webm import command


@pytest.mark.parametrize("encoder,codec,container", [("libmp3lame","mp3","mp4"), ("flac","flac","mkv"),
    ("ac3","ac3","mkv"), ("eac3","eac3","mkv"), ("libvorbis","vorbis","webm"), ("libopus","opus","webm")])
def test_supported_copied_audio_codecs(tmp_path, media_tools, encoder, codec, container):
    base = clip(tmp_path / ("base.webm" if container == "webm" else "base.mp4"))
    source = tmp_path / "audio.mkv"
    command([media_tools["ffmpeg"], "-v", "error", "-i", base.path, "-c:v", "copy", "-c:a", encoder, str(source)])
    preview = plan([FileRecord.from_path(source)], container, "h264", video_action="remux")
    assert preview.counts == {"Planned": 1}, preview.items
    item = run_plan(tmp_path, preview)[0]
    assert item["state"] == "succeeded", dict(item)
    assert probe(preview.items[0].output, media_tools)["streams"][1]["codec_name"] == codec


def test_vp9_with_incompatible_audio_can_remux_without_audio(tmp_path, media_tools):
    base = clip(tmp_path / "base.webm")
    source = tmp_path / "vp9-aac.mkv"
    command([media_tools["ffmpeg"], "-v", "error", "-i", base.path, "-c:v", "copy", "-c:a", "aac", str(source)])
    preview = plan([FileRecord.from_path(source)], "webm", "vp9", video_action="remux", video_audio_mode="none", video_allow_omissions=True)
    assert run_plan(tmp_path, preview)[0]["state"] == "succeeded"


def test_flash_tone_and_variable_timing_remain_aligned(tmp_path, media_tools):
    base = clip(tmp_path / "flash.mp4", sync=True, duration=2)
    source = tmp_path / "vfr.mp4"
    command([media_tools["ffmpeg"], "-v", "error", "-i", base.path, "-vf", "select='not(mod(n,3))'",
        "-fps_mode:v", "passthrough", "-enc_time_base:v", "1:12000", "-c:v", "libx264", "-threads", "1", "-c:a", "copy", str(source)])
    preview = plan([FileRecord.from_path(source)], "mkv", "h264", video_action="remux")
    item = run_plan(tmp_path, preview)[0]
    assert item["state"] == "succeeded", dict(item)
    # Decode audio and compare onset relative to the video start. The MP4 and
    # Matroska containers express priming/padding differently, within one frame.
    onsets = []
    for path in (str(source), preview.items[0].output):
        info = probe(path, media_tools)
        raw = command([media_tools["ffmpeg"], "-v", "error", "-i", path, "-map", "0:a:0", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"])
        audio = array.array("h", raw)
        first = next(n for n, value in enumerate(audio) if abs(value) > 2000) / 16000
        onsets.append(first + info["streams"][1]["start"] - info["streams"][0]["start"])
    assert abs(onsets[1] - onsets[0]) < .03


@pytest.mark.parametrize("point", ["written", "committed"])
def test_remux_process_loss_recovers_owned_output(tmp_path, media_tools, point):
    source = clip(tmp_path / "source.mp4")
    before = Path(source.path).read_bytes()
    preview = plan([source], "mkv", "h264", video_action="remux")
    store = Store(tmp_path / "history")
    try:
        job = store.enqueue("Remux", "video", video_snapshot(preview))
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
        assert Path(source.path).read_bytes() == before
        assert not list(tmp_path.glob("*.part"))
    finally:
        store.close()
