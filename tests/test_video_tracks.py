import array
import copy
import json
from pathlib import Path

import pytest

from media_tool.domain import FileRecord
from media_tool.jobs import Store, execute_job
from media_tool.video import probe, video_snapshot, verify_output
from media_tool.video_tracks import track_numbers, verify_subtitles
from test_foundation import app, window, wait_until
from test_video import media_tools, clip, run_plan
from test_video_formats import plan
from test_webm import command


def tracks_fixture(root, tools, container="mkv", subtitle="subrip"):
    source = clip(root / "base.mkv", audio=2, duration=2)
    text = root / "words.srt"
    text.write_text("1\n00:00:00,200 --> 00:00:00,800\nHello 日本\n\n2\n00:00:01,100 --> 00:00:01,800\nSecond cue\n", encoding="utf-8")
    output = root / f"tracks.{container}"
    command([tools["ffmpeg"], "-v", "error", "-i", source.path, "-i", str(text), "-map", "0", "-map", "1:0", "-map", "1:0",
        "-c", "copy", "-c:s", subtitle, "-metadata:s:a:0", "language=eng", "-metadata:s:a:0", "title=Original",
        "-metadata:s:a:1", "language=hin", "-metadata:s:a:1", "title=Commentary", "-disposition:a:0", "default",
        "-disposition:a:1", "0", "-metadata:s:s:0", "language=eng", "-metadata:s:s:0", "title=English",
        "-metadata:s:s:1", "language=hin", "-metadata:s:s:1", "title=Hindi", "-disposition:s:0", "0",
        "-disposition:s:1", "forced", str(output)])
    return FileRecord.from_path(output)


@pytest.mark.parametrize("container,subtitle,codec", [("mkv","subrip","h264"), ("mkv","ass","hevc"),
    ("mkv","webvtt","h264"), ("mp4","mov_text","h264"), ("webm","webvtt","vp9")])
def test_multiple_tracks_order_labels_subtitles_and_tones(tmp_path, media_tools, container, subtitle, codec):
    source = tracks_fixture(tmp_path, media_tools, "mp4" if container == "mp4" else "mkv", subtitle)
    before = Path(source.path).read_bytes()
    preview = plan([source], container, codec, video_audio_mode="custom", video_audio_tracks="2, 1",
                   video_subtitle_mode="custom", video_subtitle_tracks="2, 1")
    assert preview.counts == {"Planned": 1}, preview.items
    item = run_plan(tmp_path, preview)[0]
    assert item["state"] == "succeeded", item
    output = preview.items[0].output
    info = probe(output, media_tools, audio_layout=codec == "vp9", track_details=True)
    assert len(info["streams"]) == 5
    assert [s["language"] for s in info["streams"][1:]] == ["hin", "eng", "hin", "eng"]
    assert [s["default"] for s in info["streams"][1:3]] == [0, 1]
    assert info["streams"][3]["forced"] == 1
    for ordinal, frequency in enumerate((880, 440)):
        raw = command([media_tools["ffmpeg"], "-v", "error", "-i", output, "-map", f"0:a:{ordinal}", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"])
        samples = array.array("h", raw)[4000:12000]
        crossings = sum(a < 0 <= b for a, b in zip(samples, samples[1:])) * 2
        assert abs(crossings - frequency) < 30
    damaged = copy.deepcopy(info)
    damaged["streams"][2]["language"] = "fra"
    with pytest.raises(ValueError, match="language or title"):
        verify_output(preview.items[0].video_spec, damaged)
    assert Path(source.path).read_bytes() == before


@pytest.mark.parametrize("text", ["", "0", "1,1", "1,", "-1", "100", "1 2", "1;2"])
def test_invalid_selection_numbers(text):
    with pytest.raises(ValueError):
        track_numbers(text)


def test_all_tracks_missing_tracks_and_incompatible_subtitles(tmp_path, media_tools):
    source = tracks_fixture(tmp_path, media_tools)
    assert plan([source], "mkv", "h264", video_audio_mode="all", video_subtitle_mode="all").counts == {"Planned": 1}
    assert plan([source], "mkv", "h264").counts == {"Review": 1}
    for options, message in ((dict(video_audio_mode="custom", video_audio_tracks="3"), "Audio track 3"),
                             (dict(video_subtitle_mode="custom", video_subtitle_tracks="3"), "Subtitle track 3")):
        preview = plan([source], "mkv", "h264", **options)
        assert preview.counts == {"Invalid": 1}
        assert message in preview.items[0].reason
    for container, codec in (("mp4", "h264"), ("webm", "vp9")):
        preview = plan([source], container, codec, video_audio_mode="all", video_subtitle_mode="all")
        assert preview.counts == {"Invalid": 1}
        assert "cannot keep selected subrip" in preview.items[0].reason
    silent = plan([source], "mkv", "h264", video_audio_mode="none", video_subtitle_mode="all", video_allow_omissions=True)
    assert run_plan(tmp_path, silent)[0]["state"] == "succeeded"
    assert not [s for s in probe(silent.items[0].output, media_tools)["streams"] if s["codec_type"] == "audio"]


def test_target_budget_counts_all_selected_audio_and_subtitles(tmp_path, media_tools):
    source = tracks_fixture(tmp_path, media_tools)
    preview = plan([source], "mkv", "h264", video_audio_mode="all", video_subtitle_mode="all", video_mode="target_size", video_target_size=.15)
    budget = preview.items[0].video_spec["budget"]
    assert budget["audio_count"] == 2 and budget["total_audio_bitrate"] == 256000
    assert budget["audio_bytes"] > 64000
    item = run_plan(tmp_path, preview)[0]
    assert item["state"] == "succeeded", item
    assert Path(preview.items[0].output).stat().st_size <= 150000


def test_track_controls_snapshot_and_invalidation(window):
    window._tool_changed("Video")
    assert window.plan_request().video_audio_mode == "all"
    assert window.video_audio_mode.findData("single") == -1
    assert window.plan_request().video_subtitle_mode == "none"
    window.video_audio_mode.setCurrentIndex(window.video_audio_mode.findData("custom"))
    window.video_audio_tracks.setText("2, 1")
    window.video_subtitle_mode.setCurrentIndex(window.video_subtitle_mode.findData("all"))
    assert window.video_audio_tracks.isVisible()
    request = window.plan_request()
    assert request.video_audio_tracks == "2, 1" and request.video_subtitle_mode == "all"
    window.last_plan = object()
    window.video_audio_tracks.setText("1")
    assert window.last_plan is None


def test_schema10_frozen_single_track_job_survives_upgrade(tmp_path, media_tools):
    source = clip(tmp_path / "old.mp4")
    specs = video_snapshot(plan([source], "webm", "vp9"))
    spec = specs[0]
    del spec["audio_tracks"], spec["subtitle_tracks"]
    spec["video"] = probe(source.path, media_tools, audio_layout=True)
    store = Store(tmp_path / "history")
    job = store.enqueue("Legacy single audio", "video", specs)
    store.db.execute("PRAGMA user_version=10")
    store.close()
    store = Store(tmp_path / "history")
    try:
        assert execute_job(store, job) == "succeeded", store.items(job)
    finally:
        store.close()


@pytest.mark.parametrize("point", ["written", "prepared"])
def test_multitrack_cancel_retry_and_original_protection(tmp_path, media_tools, point):
    import threading
    source = tracks_fixture(tmp_path, media_tools)
    before = Path(source.path).read_bytes()
    preview = plan([source], "mkv", "h264", video_audio_mode="all", video_subtitle_mode="all")
    store = Store(tmp_path / "history")
    cancelled = threading.Event()
    try:
        job = store.enqueue("Tracks", "video", video_snapshot(preview))
        execute_job(store, job, cancelled.is_set, hook=lambda stage: cancelled.set() if stage == point else None)
        assert store.items(job)[0]["state"] == "cancelled", store.items(job)
        assert not Path(preview.items[0].output).exists()
        assert not list(tmp_path.glob("*.part"))
        store.retry(job)
        assert execute_job(store, job) == "succeeded", store.items(job)
        assert Path(source.path).read_bytes() == before
    finally:
        store.close()


@pytest.mark.parametrize("damage,message", [("text", "content changed"), ("time", "timing changed"), ("count", "cue count"), ("unknown", "Cannot verify")])
def test_copied_subtitle_verification_rejects_changed_payload_and_timing(monkeypatch, damage, message):
    import media_tool.video_tracks as tracks
    original = [{"pts_time": "0.2", "duration_time": "0.6", "data_hash": "SHA256:original"}]
    changed = copy.deepcopy(original)
    if damage == "text":
        changed[0]["data_hash"] = "SHA256:altered"
    elif damage == "time":
        changed[0]["pts_time"] = "0.25"
    elif damage == "count":
        changed = []
    else:
        changed[0].pop("pts_time")
    monkeypatch.setattr(tracks, "subtitle_packets", lambda path, *args: original if path == "source" else changed)
    spec = dict(source="source", tools={}, video_index=0, video={"streams": [{"index": 0, "start": 0}]},
                subtitle_tracks=[{"index": 1, "codec": "subrip"}])
    info = {"streams": [{"index": 0, "codec_type": "video", "start": 0}, {"index": 1, "codec_type": "subtitle"}]}
    with pytest.raises(ValueError, match=message):
        verify_subtitles(spec, "output", info, lambda: False)


def test_all_selection_handles_silent_files_and_empty_subtitles(tmp_path, media_tools):
    source = clip(tmp_path / "silent.mp4", audio=0)
    preview = plan([source], "mkv", "h264", video_audio_mode="all", video_subtitle_mode="all")
    assert preview.counts == {"Planned": 1}
    assert run_plan(tmp_path, preview)[0]["state"] == "succeeded"
    unavailable = plan([source], "mkv", "h264", video_audio_mode="custom", video_audio_tracks="1")
    assert unavailable.counts == {"Invalid": 1}
