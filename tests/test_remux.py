import copy
from pathlib import Path

import pytest

from media_tool.video import encode_command, probe, inspect_tools
from media_tool.remux import verify_packet_pair
from test_foundation import app, window, wait_until
from test_video import media_tools, clip, run_plan
from test_video_formats import plan
from test_video_tracks import tracks_fixture
from test_external_subtitles import sidecar
from test_webm import command
from media_tool.domain import FileRecord
from media_tool.jobs import Store, execute_job
from media_tool.video import video_snapshot


@pytest.mark.parametrize("source_ext,container,audio", [("mp4","mkv",1), ("mkv","mp4",1), ("mp4","mp4",1),
    ("webm","webm",1), ("webm","mkv",1), ("mov","mp4",1), ("mkv","mkv",0)])
def test_real_remux_packet_identity_timing_decode_and_original(tmp_path, media_tools, source_ext, container, audio):
    source = clip(tmp_path / f"Original 日本.{source_ext}", audio=audio)
    before = Path(source.path).read_bytes()
    preview = plan([source], container, "h264", video_action="remux")
    assert preview.counts == {"Planned": 1}, preview.items
    spec = preview.items[0].video_spec
    args = encode_command(spec, tmp_path / "unused.part")
    assert args[args.index("-c:v")+1] == "copy"
    assert not set(("-crf", "-preset", "-vf", "-b:v", "-b:a", "-ar", "-pix_fmt")) & set(args)
    item = run_plan(tmp_path, preview)[0]
    assert item["state"] == "succeeded", dict(item)
    assert "without re-encoding" in item["message"]
    assert Path(source.path).read_bytes() == before
    assert not list(tmp_path.glob("*.part"))


def test_remux_preserves_selected_audio_order_and_existing_subtitles(tmp_path, media_tools):
    source = tracks_fixture(tmp_path, media_tools)
    preview = plan([source], "mkv", "h264", video_action="remux", video_audio_mode="custom", video_audio_tracks="2,1", video_subtitle_mode="all")
    assert preview.counts == {"Planned": 1}, preview.items
    item = run_plan(tmp_path, preview)[0]
    assert item["state"] == "succeeded", dict(item)
    info = probe(preview.items[0].output, media_tools, track_details=True)
    assert [s["language"] for s in info["streams"][1:3]] == ["hin", "eng"]
    assert len(info["streams"]) == 5


@pytest.mark.parametrize("container", ["mp4", "mkv"])
def test_external_subtitle_with_copied_video_audio(tmp_path, media_tools, container):
    source = clip(tmp_path / "video.mp4", duration=2)
    subtitle = sidecar(tmp_path)
    preview = plan([source], container, "h264", video_action="remux", video_external_enabled=True, video_external_subtitle=str(subtitle))
    assert preview.counts == {"Planned": 1}, preview.items
    item = run_plan(tmp_path, preview)[0]
    assert item["state"] == "succeeded", dict(item)
    assert len(probe(preview.items[0].output, media_tools)["streams"]) == 3


def test_ui_remux_hides_encoding_controls_and_restores_values(window):
    window._tool_changed("Video")
    window.resize_controls.mode.setCurrentIndex(window.resize_controls.mode.findData("percent"))
    window.compression_controls.mode.setCurrentIndex(window.compression_controls.mode.findData("target_size"))
    action = window.video_format_controls.action
    action.setCurrentIndex(action.findData("remux"))
    request = window.plan_request()
    assert request.video_action == "remux" and request.resize_mode == "none" and request.video_mode == "quality"
    assert window.start_rename_button.text() == "Start Remux"
    assert not window.resize_controls.panel.isVisible()
    assert not window.compression_controls.panel.isVisible()
    assert not window.video_speed.isVisible()
    assert not window.video_format_controls.codec.isVisible()
    action.setCurrentIndex(action.findData("convert"))
    assert window.resize_controls.mode.currentData() == "percent"
    assert window.compression_controls.mode.currentData() == "target_size"
    assert window.video_speed.isVisible()


def test_compatibility_omissions_and_no_encoder_requirement(tmp_path, media_tools, monkeypatch):
    import media_tool.video as video
    source = clip(tmp_path / "video.mp4", audio=2)
    preview = plan([source], "webm", "vp9", video_action="remux")
    assert preview.counts == {"Invalid": 1}
    assert "cannot keep h264 video" in preview.items[0].reason
    assert plan([source], "mkv", "h264", video_action="remux").counts == {"Review": 1}
    real = video.run_process
    def no_encoding_checks(args, *a, **kw):
        assert "-encoders" not in args
        return real(args, *a, **kw)
    monkeypatch.setattr(video, "run_process", no_encoding_checks)
    preview = plan([source], "mkv", "h264", video_action="remux", video_audio_mode="all")
    assert preview.counts == {"Planned": 1}
    vp9 = tmp_path / "vp9-aac.mkv"
    command([media_tools["ffmpeg"], "-v", "error", "-i", source.path, "-c:v", "libvpx-vp9", "-threads", "1", "-c:a", "copy", str(vp9)])
    preview = plan([FileRecord.from_path(vp9)], "webm", "vp9", video_action="remux")
    assert preview.counts == {"Invalid": 1}
    assert "cannot keep aac audio" in preview.items[0].reason
    for options in (dict(resize_mode="percent"), dict(video_mode="target_size")):
        with pytest.raises(ValueError, match="Remux copies"):
            plan([], "mp4", "h264", video_action="remux", **options)


def test_hevc_and_non_square_pixels_are_copied(tmp_path, media_tools):
    source = clip(tmp_path / "base.mp4", audio=0)
    hevc = tmp_path / "hevc.mkv"
    command([media_tools["ffmpeg"], "-v", "error", "-i", source.path, "-vf", "setsar=4/3", "-c:v", "libx265",
        "-x265-params", "pools=none:frame-threads=1:log-level=error", str(hevc)])
    preview = plan([FileRecord.from_path(hevc)], "mp4", "h264", video_action="remux")
    assert preview.counts == {"Planned": 1}, preview.items
    item = run_plan(tmp_path, preview)[0]
    assert item["state"] == "succeeded", dict(item)
    output = probe(preview.items[0].output, media_tools)["streams"][0]
    assert output["codec_name"] == "hevc" and output["sample_aspect_ratio"] == "4:3"


def test_rotation_preserved_in_mp4_and_incompatible_container_explained(tmp_path, media_tools):
    source = clip(tmp_path / "base.mp4", audio=0)
    rotated = tmp_path / "rotated.mov"
    command([media_tools["ffmpeg"], "-v", "error", "-display_rotation:v:0", "90", "-i", source.path, "-c", "copy", str(rotated)])
    record = FileRecord.from_path(rotated)
    preview = plan([record], "mkv", "h264", video_action="remux")
    assert preview.counts == {"Invalid": 1} and "rotation only in MP4" in preview.items[0].reason
    preview = plan([record], "mp4", "h264", video_action="remux")
    item = run_plan(tmp_path, preview)[0]
    assert item["state"] == "succeeded", dict(item)
    assert probe(preview.items[0].output, media_tools)["streams"][0]["rotation"] == 90


@pytest.mark.parametrize("damage,message", [("hash", "payload changed"), ("pts_time", "timing changed"), ("duration_time", "duration changed")])
def test_packet_verifier_rejects_payload_and_clock_changes(damage, message):
    before = dict(hash="SHA256:original", pts_time=1., dts_time=.9, duration_time=.1)
    after = dict(before)
    after[damage] = "SHA256:changed" if damage == "hash" else after[damage] + .1
    with pytest.raises(ValueError, match=message):
        verify_packet_pair(before, after, 0, 0, "video", 2, False)


def test_cancel_during_packet_verification_cleans_workers_and_retry_succeeds(tmp_path, media_tools, monkeypatch):
    import threading
    import media_tool.remux as remux
    source = clip(tmp_path / "longer.mp4", duration=5)
    preview = plan([source], "mkv", "h264", video_action="remux")
    store = Store(tmp_path / "history")
    cancelled = threading.Event()
    original = remux.verify_packet_pair
    def cancel_after_packet(*args):
        original(*args)
        cancelled.set()
    try:
        job = store.enqueue("Remux", "video", video_snapshot(preview))
        monkeypatch.setattr(remux, "verify_packet_pair", cancel_after_packet)
        execute_job(store, job, cancelled.is_set)
        assert store.items(job)[0]["state"] == "cancelled", store.items(job)
        assert not Path(preview.items[0].output).exists()
        assert not any(t.name == "remux-packet-source" for t in threading.enumerate())
        assert not list(tmp_path.glob("*.part"))
        monkeypatch.setattr(remux, "verify_packet_pair", original)
        store.retry(job)
        assert execute_job(store, job) == "succeeded", store.items(job)
    finally:
        store.close()


def test_late_collision_and_schema12_conversion_job(tmp_path, media_tools):
    source = clip(tmp_path / "source.mp4")
    preview = plan([source], "mkv", "h264", video_action="remux")
    output = Path(preview.items[0].output)
    item = run_plan(tmp_path, preview, hook=lambda stage: output.write_bytes(b"unrelated") if stage == "prepared" else None)[0]
    assert item["state"] == "failed" and output.read_bytes() == b"unrelated"
    store = Store(tmp_path / "old-history")
    old = store.enqueue("Old convert", "video", video_snapshot(plan([source], "mp4", "h264")))
    store.db.execute("PRAGMA user_version=12")
    store.close()
    store = Store(tmp_path / "old-history")
    try:
        assert execute_job(store, old) == "succeeded", store.items(old)
    finally:
        store.close()
