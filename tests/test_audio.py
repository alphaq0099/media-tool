import array
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from media_tool import audio
from media_tool.domain import FileRecord
from media_tool.jobs import Store, execute_job
from media_tool.planning import PlanRequest
from media_tool.video import probe
from test_foundation import app, window, wait_until


@pytest.fixture(scope="module")
def tools():
    return audio.inspect_tools()


def ffmpeg(args):
    subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", *map(str, args)],
                   check=True, capture_output=True, timeout=30,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def tone(path, channels=1, rate=48000, duration=.8):
    ffmpeg(["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate={rate}", "-t", duration,
            "-ac", channels, "-c:a", "pcm_s24le", path])
    return FileRecord.from_path(path)


def plan(records, extension="mp3", **settings):
    return audio.plan_audio(records, PlanRequest("audio", extension=extension, **settings))


def run(tmp_path, preview, **options):
    with_store = Store(tmp_path / "history")
    try:
        job = with_store.enqueue("Audio", "audio", audio.audio_snapshot(preview))
        execute_job(with_store, job, **options)
        return with_store.items(job)
    finally:
        with_store.close()


@pytest.mark.parametrize("extension", list(audio.FORMATS))
def test_real_formats_unicode_and_original_identity(tmp_path, tools, extension):
    source = tone(tmp_path / "音声 ' [1].wav")
    before = hashlib.sha256(Path(source.path).read_bytes()).hexdigest()
    preview = plan([source], extension)
    assert preview.counts == {"Planned": 1}, preview.items
    items = run(tmp_path, preview)
    assert items[0]["state"] == "succeeded", items
    info = probe(preview.items[0].output, tools, audio_layout=True)
    assert len(info["streams"]) == 1
    assert info["streams"][0]["codec_name"] == audio.FORMATS[extension][2]
    assert hashlib.sha256(Path(source.path).read_bytes()).hexdigest() == before
    assert Path(source.path).stat().st_mtime_ns == source.modified_ns
    assert Path(source.path).stat().st_ino == source.inode
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize("extension", list(audio.FORMATS))
def test_copy_payloads_verified_for_each_format(tmp_path, tools, extension):
    source = tone(tmp_path / "tone.wav")
    converted = plan([source], extension)
    assert run(tmp_path, converted)[0]["state"] == "succeeded"
    encoded = FileRecord.from_path(converted.items[0].output)
    copied = plan([encoded], extension, audio_mode="copy")
    assert copied.counts == {"Planned": 1}, copied.items
    results = run(tmp_path, copied)
    assert results[0]["state"] == "succeeded", results
    assert Path(encoded.path).exists()


def multiple(path, silent=False):
    args = ["-f", "lavfi", "-i", "color=black:size=32x32:rate=10"]
    if not silent:
        for freq in (440, 880):
            args += ["-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000:duration=0.8"]
    args += ["-map", "0:v:0"]
    if not silent:
        args += ["-map", "1:a:0", "-map", "2:a:0", "-c:a", "aac", "-metadata:s:a:0", "language=eng",
                 "-metadata:s:a:1", "title=Second tone"]
    args += ["-t", "0.8", "-c:v", "libx264", "-threads", "1", path]
    ffmpeg(args)
    return FileRecord.from_path(path)


def test_multi_track_selection_naming_and_decoded_signal(tmp_path, tools):
    source = multiple(tmp_path / "movie.mkv")
    all_tracks = plan([source], "flac")
    assert all_tracks.counts == {"Planned": 2}, all_tracks.items
    assert [Path(i.output).name for i in all_tracks.items] == ["movie_track01.flac", "movie_track02.flac"]
    selected = plan([source], "wav", audio_tracks_mode="custom", audio_tracks="2")
    assert len(selected.items) == 1 and selected.items[0].audio_spec["track"] == 2
    items = run(tmp_path, selected)
    assert items[0]["state"] == "succeeded", items
    raw = subprocess.check_output([tools["ffmpeg"], "-v", "error", "-i", selected.items[0].output,
                                  "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    samples = array.array("h", raw)[2000:10000]
    crossings = sum(a < 0 <= b for a, b in zip(samples, samples[1:]))
    assert 860 < crossings * 2 < 900
    missing = plan([source], audio_tracks_mode="custom", audio_tracks="3")
    assert missing.items[0].status == "Invalid" and "unavailable" in missing.items[0].reason


def test_no_audio_rejected_without_writing(tmp_path, tools):
    source = multiple(tmp_path / "silent.mkv", silent=True)
    preview = plan([source])
    assert preview.items[0].status == "Invalid" and "no audio" in preview.items[0].reason
    with pytest.raises(ValueError):
        audio.audio_snapshot(preview)
    assert not list(tmp_path.glob("*.mp3"))


@pytest.mark.parametrize("extension", ["m4a", "mp3", "opus"])
def test_bitrate_resampling_downmix(tmp_path, tools, extension):
    source = tone(tmp_path / "surround.wav", channels=6, rate=96000)
    preview = plan([source], extension, audio_rate_mode="bitrate", audio_bitrate=128,
                   audio_channels="stereo", audio_sample_rate=48000)
    assert preview.counts == {"Planned": 1}, preview.items
    results = run(tmp_path, preview)
    assert results[0]["state"] == "succeeded", results
    info = audio.audio_info(preview.items[0].output, tools)
    assert info["streams"][0]["channels"] == 2
    assert info["streams"][0]["sample_rate"] == "48000"


@pytest.mark.parametrize("extension", ["flac", "wav", "m4a", "opus"])
def test_preserves_known_surround_layout(tmp_path, tools, extension):
    source = tone(tmp_path / "surround.wav", channels=6)
    preview = plan([source], extension)
    results = run(tmp_path, preview)
    assert results[0]["state"] == "succeeded", results
    assert preview.items[0].audio_spec["settings"]["channels"] == 6


def test_copy_requires_compatible_codec_and_no_processing(tmp_path, tools):
    source = tone(tmp_path / "tone.wav")
    preview = plan([source], "mp3", audio_mode="copy")
    assert "Cannot copy" in preview.items[0].reason
    with pytest.raises(ValueError, match="cannot change"):
        plan([source], "wav", audio_mode="copy", audio_channels="mono")


def test_per_track_skip_and_collision_policy(tmp_path, tools):
    source = multiple(tmp_path / "movie.mkv")
    existing = tmp_path / "movie_track01.mp3"
    existing.write_bytes(b"keep me")
    preview = plan([source], collision="skip")
    assert preview.counts == {"Skipped": 1, "Planned": 1}
    results = run(tmp_path, preview)
    assert [i["state"] for i in results] == ["skipped", "succeeded"]
    assert existing.read_bytes() == b"keep me"
    unique = plan([source])
    assert all("_1.mp3" in i.output for i in unique.items)


def test_late_collision_and_changed_source_fail_safely(tmp_path, tools):
    source = tone(tmp_path / "tone.wav")
    preview = plan([source])
    output = Path(preview.items[0].output)
    output.write_bytes(b"late arrival")
    results = run(tmp_path, preview)
    assert results[0]["state"] == "failed" and output.read_bytes() == b"late arrival"
    fresh = plan([source])
    with Path(source.path).open("ab") as f:
        f.write(b"changed")
    results = run(tmp_path, fresh)
    assert results[0]["state"] == "failed" and "changed" in results[0]["message"]
    assert not Path(fresh.items[0].output).exists()


def test_cancel_and_retry_clean_owned_partial(tmp_path, tools):
    source = tone(tmp_path / "tone.wav")
    preview = plan([source])
    cancel = False
    def hook(point):
        nonlocal cancel
        if point == "created":
            cancel = True
    store = Store(tmp_path / "history")
    try:
        job = store.enqueue("Audio", "audio", audio.audio_snapshot(preview))
        execute_job(store, job, cancelled=lambda: cancel, hook=hook)
        assert store.items(job)[0]["state"] == "cancelled"
        assert not Path(preview.items[0].output).exists()
        assert not list(tmp_path.glob("*.part"))
        assert store.retry(job) == 1
        execute_job(store, job)
        assert store.items(job)[0]["state"] == "succeeded"
    finally:
        store.close()


@pytest.mark.parametrize("point", ["created", "prepared", "committed"])
def test_real_process_loss_and_recovery(tmp_path, tools, point):
    source = tone(tmp_path / "tone.wav")
    preview = plan([source])
    store = Store(tmp_path / "history")
    try:
        job = store.enqueue("Audio", "audio", audio.audio_snapshot(preview))
        code = """import os,sys
from media_tool.jobs import Store,execute_job
store=Store(sys.argv[1])
def hook(point):
    if point==sys.argv[3]: os._exit(23)
execute_job(store,sys.argv[2],hook=hook)
"""
        child = subprocess.run([sys.executable, "-c", code, str(store.directory), job, point],
                               env=dict(os.environ, PYTHONPATH=str(Path("src").resolve())), timeout=30,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        assert child.returncode == 23
        store.recover()
        assert store.items(job)[0]["state"] == "interrupted"
        assert store.retry(job) == 1
        execute_job(store, job)
        assert store.items(job)[0]["state"] == "succeeded", store.items(job)
        assert not list(tmp_path.glob("*.part"))
        assert len(list(tmp_path.glob("*.mp3"))) == 1
    finally:
        store.close()


def test_missing_encoder_and_missing_tools(monkeypatch):
    real_resolve = audio.resolve_tool
    monkeypatch.setattr(audio, "resolve_tool", lambda name: None)
    with pytest.raises(ValueError, match="required"):
        audio.inspect_tools()
    monkeypatch.setattr(audio, "resolve_tool", real_resolve)
    monkeypatch.setattr(audio, "run_process", lambda *a, **kw: " A..... different_encoder\n")
    with pytest.raises(ValueError, match="no libmp3lame"):
        audio.inspect_tools()


def test_ui_shared_flow_and_selective_intake(window, tmp_path, tools):
    source = tone(tmp_path / "one.wav")
    other = tone(tmp_path / "two.wav")
    window._tool_changed("Audio")
    window.model.append([source, other])
    window.table.selectRow(0)
    window.audio_controls.channels.setCurrentIndex(2)
    queue = window.ensure_jobs()
    window.start_rename_button.click()
    wait_until(lambda: window.plan_worker is None and queue.worker is None and window.tabs.currentIndex() == 2, 30000)
    assert len(window.result_model.items) == 1
    assert window.result_model.items[0]["state"] == "succeeded", window.result_model.items
    assert not window.result_can_undo
    assert window.model.rowCount() == 1 and window.model.items[0].path == other.path
    assert window.open_video_button.isVisible()
    assert Path(source.path).exists()


def test_ui_audio_controls_themes_and_track_picker(window, tmp_path, tools):
    from PySide6.QtCore import QTimer, Qt
    from PySide6.QtWidgets import QApplication, QDialog, QListWidget
    source = multiple(tmp_path / "tracks.mkv")
    window._tool_changed("Audio")
    window.category_filter.setCurrentText("Video sources")
    window.model.append([source])
    controls = window.audio_controls
    for theme in ("light", "dark"):
        window.set_theme(theme)
        assert controls.panel.isVisible() and not window.video_panel.isVisible()
        assert not window.detail.isVisible() and not window.keep.isVisible()
        assert controls.quality.isVisible() and not controls.bitrate.isVisible()
    controls.tracks.setCurrentIndex(2)
    accepted = []
    def accept_picker():
        dialog = QApplication.activeModalWidget()
        if dialog is None:
            QTimer.singleShot(10, accept_picker)
            return
        listing = dialog.findChild(QListWidget)
        listing.item(1).setCheckState(Qt.CheckState.Checked)
        accepted.append(True)
        dialog.accept()
    QTimer.singleShot(10, accept_picker)
    controls.choose.click()
    wait_until(lambda: accepted and controls.worker is None, 15000)
    assert controls.options()["audio_tracks"] == "2"
    controls.mode.setCurrentIndex(1)
    assert not controls.quality.isVisible() and not controls.sample_rate.isEnabled()
    assert controls.options()["audio_channels"] == "preserve"
    assert controls.channels.currentData() == "preserve"
    window._tool_changed("Images")
    assert not controls.panel.isVisible() and window.image_quality.isVisible()


def test_ui_extracts_video_sources_after_input_filter_selection(window,tmp_path,tools):
    source=multiple(tmp_path/'extract.mkv')
    window._tool_changed('Audio')
    window.model.append([source])
    assert window.proxy.rowCount()==0
    window.category_filter.setCurrentText('Video sources')
    assert window.proxy.rowCount()==1
    queue=window.ensure_jobs()
    window.start_rename_button.click()
    wait_until(lambda:window.plan_worker is None and queue.worker is None and window.tabs.currentIndex()==2,30000)
    assert window.result_model.items[0]['state']=='succeeded',window.result_model.items
    assert Path(source.path).exists()
    assert Path(window.result_model.items[0]['output']).suffix=='.mp3'


def test_ui_mixed_audio_and_video_inputs_remain_convertible(window,tmp_path,tools):
    sound=tone(tmp_path/'sound.wav')
    movie=multiple(tmp_path/'movie.mkv')
    window._tool_changed('Audio')
    window.model.append([sound,movie])
    window.category_filter.setCurrentText('Audio + video')
    assert window.proxy.rowCount()==2
    queue=window.ensure_jobs()
    window.start_rename_button.click()
    wait_until(lambda:window.plan_worker is None and queue.worker is None and window.tabs.currentIndex()==2,30000)
    # Default All tracks extracts two movie tracks plus the WAV's one track.
    assert len(window.result_model.items)==3
    assert all(item['state']=='succeeded' for item in window.result_model.items),window.result_model.items
    assert Path(sound.path).exists() and Path(movie.path).exists()


def test_ui_mixed_audio_and_video_inputs_can_join(window,tmp_path,tools):
    sound=tone(tmp_path/'sound.wav')
    movie=multiple(tmp_path/'movie.mkv')
    window._tool_changed('Audio')
    window.model.append([sound,movie])
    window.category_filter.setCurrentText('Audio + video')
    window.audio_controls.task.setCurrentIndex(window.audio_controls.task.findData('join'))
    window.format.setCurrentText('WAV')
    queue=window.ensure_jobs()
    window.start_rename_button.click()
    wait_until(lambda:window.plan_worker is None and queue.worker is None and window.tabs.currentIndex()==2,30000)
    assert len(window.result_model.items)==1
    assert window.result_model.items[0]['state']=='succeeded',window.result_model.items
    assert Path(sound.path).exists() and Path(movie.path).exists()


@pytest.mark.parametrize("extension", ["flac", "wav"])
def test_lossless_24bit_samples_round_trip(tmp_path, tools, extension):
    source = tone(tmp_path / "original.wav", channels=2)
    preview = plan([source], extension)
    results = run(tmp_path, preview)
    assert results[0]["state"] == "succeeded", results
    def decoded(path):
        return subprocess.check_output([tools["ffmpeg"], "-v", "error", "-i", str(path), "-map", "0:a:0",
                                        "-c:a", "pcm_s24le", "-f", "s24le", "-"],
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert decoded(source.path) == decoded(preview.items[0].output)


@pytest.mark.parametrize("copying", [False, True])
def test_selected_track_offsets_and_durations(tmp_path, tools, copying):
    source = tmp_path / "offset.mkv"
    ffmpeg(["-f", "lavfi", "-i", "color=size=32x32:rate=10:duration=4",
            "-itsoffset", "1", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=0.8",
            "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-threads", "1", "-c:a", "aac", source])
    preview = plan([FileRecord.from_path(source)], "m4a", audio_mode="copy" if copying else "encode")
    assert preview.counts == {"Planned": 1}, preview.items
    assert preview.items[0].audio_spec["duration"] < 1
    results = run(tmp_path, preview)
    assert results[0]["state"] == "succeeded", results
    info = audio.audio_info(preview.items[0].output, tools)
    assert abs(info["streams"][0]["start"] or 0) < .05
    assert info["duration"] < 1.1


@pytest.mark.parametrize("extension", ["mp3", "m4a", "flac", "wav", "opus"])
def test_metadata_and_cover_art_policy(tmp_path, tools, extension):
    plain = tone(tmp_path / "plain.wav")
    cover = tmp_path / "cover.png"
    from PIL import Image
    Image.new("RGB", (32, 32), "red").save(cover)
    tagged = tmp_path / "tagged.mp3"
    ffmpeg(["-i", plain.path, "-i", cover, "-map", "0:a", "-map", "1:v", "-c:a", "libmp3lame",
            "-c:v", "png", "-disposition:v:0", "attached_pic", "-metadata", "title=Private title",
            "-metadata", "artist=Private artist", "-metadata", "comment=Private comment", tagged])
    original = tagged.read_bytes()
    preview = plan([FileRecord.from_path(tagged)], extension)
    results = run(tmp_path, preview)
    assert results[0]["state"] == "succeeded", results
    info = json.loads(subprocess.check_output([tools["ffprobe"], "-v", "error", "-show_streams", "-show_format",
                                               "-of", "json", preview.items[0].output],
                                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))
    assert len(info["streams"]) == 1 and info["streams"][0]["codec_type"] == "audio"
    for section in (info["format"], *info["streams"]):
        assert not ({"title", "artist", "comment", "album"} & {k.lower() for k in section.get("tags", {})})
    assert tagged.read_bytes() == original


def test_queue_restores_saved_audio_results_and_skipped_tracks(window, tmp_path, tools):
    source = multiple(tmp_path / "movie.mkv")
    (tmp_path / "movie_track01.mp3").write_bytes(b"kept")
    preview = plan([source], collision="skip")
    queue = window.ensure_jobs()
    job = queue.store.enqueue("Audio", "audio", audio.audio_snapshot(preview))
    execute_job(queue.store, job)
    queue.refresh(job)
    assert queue.archive_results_button.text() == "Audio results"
    queue.show_archive_results()
    assert window.tool == "Audio" and window.tabs.currentIndex() == 2
    assert [i["state"] for i in window.result_model.items] == ["skipped", "succeeded"]
    assert not window.result_can_undo


def test_cancellation_during_ffmpeg_progress_keeps_original(tmp_path, tools):
    source = tone(tmp_path / "long.wav", duration=10)
    preview = plan([source])
    cancel = False
    def hook(point):
        nonlocal cancel
        if point == "progress":
            cancel = True
    results = run(tmp_path, preview, cancelled=lambda: cancel, hook=hook)
    assert results[0]["state"] == "cancelled", results
    assert Path(source.path).exists() and not Path(preview.items[0].output).exists()
    assert not list(tmp_path.glob("*.part"))


def test_audio_extraction_does_not_depend_on_video_color_support(tmp_path, tools):
    source = tmp_path / "hdr-labelled.mkv"
    ffmpeg(["-f", "lavfi", "-i", "color=size=32x32:rate=10:duration=0.8", "-f", "lavfi", "-i",
            "sine=frequency=440:sample_rate=48000:duration=0.8", "-c:v", "libx264", "-threads", "1",
            "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-c:a", "aac", source])
    preview = plan([FileRecord.from_path(source)], "flac")
    assert preview.counts == {"Planned": 1}, preview.items
    assert run(tmp_path, preview)[0]["state"] == "succeeded"
