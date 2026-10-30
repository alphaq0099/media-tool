import json
from pathlib import Path

import pytest

from media_tool.video import probe, video_snapshot
from media_tool.jobs import SCHEMA, Store, execute_job
from test_foundation import app, window, wait_until
from test_video import media_tools, clip, run_plan
from test_video_formats import plan
from test_video_tracks import tracks_fixture


def sidecar(path, extension="srt"):
    path = path / f"English captions 日本.{extension}"
    text = ("1\n00:00:00,200 --> 00:00:00,800\nHello 日本\n\n2\n00:00:01,100 --> 00:00:01,800\nSecond line\n" if extension == "srt" else
            "WEBVTT\n\n00:00:00.200 --> 00:00:00.800\nHello 日本\n\n00:00:01.100 --> 00:00:01.800\nSecond line\n")
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize("container,codec", [("mp4", "h264"), ("mkv", "hevc"), ("webm", "vp9")])
@pytest.mark.parametrize("extension", ["srt", "vtt"])
def test_external_subtitle_embeds_with_verified_text_timing_and_originals(tmp_path, media_tools, container, codec, extension):
    source = clip(tmp_path / "video.mkv", duration=2)
    subtitle = sidecar(tmp_path, extension)
    before = [Path(source.path).read_bytes(), subtitle.read_bytes()]
    preview = plan([source], container, codec, video_external_enabled=True, video_external_subtitle=str(subtitle))
    assert preview.counts == {"Planned": 1}, preview.items
    item = run_plan(tmp_path, preview)[0]
    assert item["state"] == "succeeded", item
    info = probe(preview.items[0].output, media_tools, track_details=True)
    assert len(info["streams"]) == 3
    assert info["streams"][-1]["codec_name"] == {"mp4": "mov_text", "mkv": "subrip", "webm": "webvtt"}[container]
    assert info["streams"][-1]["title"] == subtitle.stem
    assert before == [Path(source.path).read_bytes(), subtitle.read_bytes()]


def test_ui_explains_audio_number_and_selects_external_file(window, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog
    window._tool_changed("Video")
    assert window.video_audio_mode.findData("single") == -1
    assert window.video_audio_mode.currentData() == "all"
    path = sidecar(tmp_path)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(path), ""))
    window.video_external_enabled.setChecked(True)
    window.video_external_browse.click()
    assert window.video_external_panel.isVisible()
    request = window.plan_request()
    assert request.video_external_enabled and request.video_external_subtitle == str(path)
    window.last_plan = object()
    window.video_external_enabled.setChecked(False)
    assert window.last_plan is None
    assert not window.plan_request().video_external_enabled


@pytest.mark.parametrize("point", ["before", "written", "prepared"])
def test_subtitle_changed_since_preview_never_commits(tmp_path, media_tools, point):
    import os
    source = clip(tmp_path / "video.mp4", duration=2)
    subtitle = sidecar(tmp_path)
    preview = plan([source], "mp4", "h264", video_external_enabled=True, video_external_subtitle=str(subtitle))
    stat = subtitle.stat()
    def change():
        subtitle.write_bytes(subtitle.read_bytes().replace(b"Hello", b"Jello"))
        os.utime(subtitle, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    if point == "before":
        change()
    item = run_plan(tmp_path, preview, hook=lambda stage: change() if stage == point else None)[0]
    assert item["state"] == "failed", item
    assert "External subtitle changed" in item["message"]
    assert not Path(preview.items[0].output).exists()
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize("codec", ["h264", "hevc"])
def test_external_with_kept_subtitles_and_target_size(tmp_path, media_tools, codec):
    source = tracks_fixture(tmp_path, media_tools)
    subtitle = sidecar(tmp_path)
    preview = plan([source], "mkv", codec, video_audio_mode="all", video_subtitle_mode="all",
        video_external_enabled=True, video_external_subtitle=str(subtitle), video_mode="target_size", video_target_size=.15)
    assert preview.counts == {"Planned": 1}, preview.items
    item = run_plan(tmp_path, preview)[0]
    assert item["state"] == "succeeded", item
    info = probe(preview.items[0].output, media_tools)
    assert len(info["streams"]) == 6
    assert Path(preview.items[0].output).stat().st_size <= 150000


def test_external_cancel_retry_and_legacy_schema11_job(tmp_path, media_tools):
    import threading
    source = clip(tmp_path / "video.mp4", duration=2)
    subtitle = sidecar(tmp_path)
    preview = plan([source], "webm", "vp9", video_external_enabled=True, video_external_subtitle=str(subtitle))
    store = Store(tmp_path / "history")
    cancelled = threading.Event()
    try:
        old = store.enqueue("Old tracks", "video", video_snapshot(plan([source], "mkv", "h264")))
        store.db.execute("PRAGMA user_version=11")
        store.close()
        store = Store(tmp_path / "history")
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA
        assert execute_job(store, old) == "succeeded", store.items(old)
        job = store.enqueue("External", "video", video_snapshot(preview))
        execute_job(store, job, cancelled.is_set, hook=lambda stage: cancelled.set() if stage == "written" else None)
        assert store.items(job)[0]["state"] == "cancelled", store.items(job)
        assert not Path(preview.items[0].output).exists()
        store.retry(job)
        assert execute_job(store, job) == "succeeded", store.items(job)
    finally:
        store.close()


@pytest.mark.parametrize("kind,message", [("empty", "nonempty"), ("encoding", "UTF-8"), ("format", "support UTF-8"), ("missing", "Choose an external"), ("corrupt", "Media tool failed|no readable cues")])
def test_bad_external_files_block_before_output(tmp_path, media_tools, kind, message):
    source = clip(tmp_path / "video.mp4", duration=2)
    subtitle = sidecar(tmp_path)
    if kind == "empty":
        subtitle.write_bytes(b"")
    elif kind == "encoding":
        subtitle.write_bytes(b"\xff\xfehello")
    elif kind == "format":
        subtitle = subtitle.rename(subtitle.with_suffix(".ass"))
    elif kind == "corrupt":
        subtitle.write_text("This is not a subtitle.", encoding="utf-8")
    with pytest.raises((ValueError, OSError), match=message):
        plan([source], "mp4", "h264", video_external_enabled=True, video_external_subtitle="" if kind == "missing" else str(subtitle))
    assert not list(tmp_path.glob("*.part"))


def test_subtitles_beyond_video_and_overlapping_mp4_cues_are_explained(tmp_path, media_tools):
    source = clip(tmp_path / "video.mp4", duration=1)
    subtitle = sidecar(tmp_path)
    preview = plan([source], "mp4", "h264", video_external_enabled=True, video_external_subtitle=str(subtitle))
    assert preview.counts == {"Invalid": 1}
    assert "extend beyond" in preview.items[0].reason
    subtitle.write_text(subtitle.read_text(encoding="utf-8").replace("00:00:01,100", "00:00:00,500"), encoding="utf-8")
    with pytest.raises(ValueError, match="non-overlapping"):
        plan([source], "mp4", "h264", video_external_enabled=True, video_external_subtitle=str(subtitle))
