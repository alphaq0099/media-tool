import json
from pathlib import Path

from media_tool.domain import FileRecord
from media_tool.video import probe, video_snapshot
from media_tool.jobs import Store, execute_job
from test_video import media_tools, clip, run_plan
from test_video_formats import plan
from test_video_tracks import tracks_fixture
from test_webm import command


def test_mp4_named_handlers_survive_track_reordering(tmp_path, media_tools):
    base = tracks_fixture(tmp_path, media_tools, "mp4", "mov_text")
    source = tmp_path / "named.mp4"
    command([media_tools["ffmpeg"], "-v", "error", "-i", base.path, "-map", "0", "-c", "copy",
        "-metadata:s:a:0", "handler_name=English original", "-metadata:s:a:1", "handler_name=Hindi commentary",
        "-metadata:s:s:0", "handler_name=English captions", "-metadata:s:s:1", "handler_name=Hindi forced", str(source)])
    preview = plan([FileRecord.from_path(source)], "mp4", "h264", video_audio_mode="custom", video_audio_tracks="2,1",
        video_subtitle_mode="custom", video_subtitle_tracks="2,1")
    item = run_plan(tmp_path, preview)[0]
    assert item["state"] == "succeeded", item
    info = probe(preview.items[0].output, media_tools, track_details=True)
    assert [s["title"] for s in info["streams"][1:]] == ["Hindi commentary", "English original", "Hindi forced", "English captions"]


def test_multiple_opus_tracks_use_independent_layout_rates_and_resampling(tmp_path, media_tools):
    source = tmp_path / "layouts.mkv"
    command([media_tools["ffmpeg"], "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=12",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=5.1",
        "-map", "0:v", "-map", "1:a", "-map", "2:a", "-t", "1", "-c:v", "libx264", "-threads", "1", "-c:a", "flac", str(source)])
    preview = plan([FileRecord.from_path(source)], "webm", "vp9", video_audio_mode="all")
    assert [s["bitrate"] for s in preview.items[0].video_spec["audio_tracks"]] == [128000, 256000]
    item = run_plan(tmp_path, preview)[0]
    assert item["state"] == "succeeded", item
    audio = probe(preview.items[0].output, media_tools, audio_layout=True)["streams"][1:]
    assert [(s["channels"], s["channel_layout"], s["sample_rate"]) for s in audio] == [(2, "stereo", "48000"), (6, "5.1", "48000")]


def test_legacy_aac_single_track_snapshot_retains_old_probe_and_mapping(tmp_path, media_tools):
    source = clip(tmp_path / "older.mkv", audio=2)
    specs = video_snapshot(plan([source], "mp4", "h264", video_audio=2, video_allow_omissions=True))
    spec = specs[0]
    del spec["audio_tracks"], spec["subtitle_tracks"], spec["codec"], spec["container"]
    spec["video"] = probe(source.path, media_tools)
    store = Store(tmp_path / "history")
    try:
        job = store.enqueue("Old AAC", "video", specs)
        assert execute_job(store, job) == "succeeded", store.items(job)
        info = probe(spec["output"], media_tools)
        assert len(info["streams"]) == 2 and info["streams"][1]["codec_name"] == "aac"
    finally:
        store.close()
