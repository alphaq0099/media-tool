import array
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from media_tool.audio import plan_audio, audio_snapshot, inspect_tools, audio_info
from media_tool.audio_edit import seconds
from media_tool.domain import FileRecord
from media_tool.jobs import Store, execute_job
from media_tool.planning import PlanRequest
from test_foundation import app, window, wait_until


@pytest.fixture(scope="module")
def tools():
    return inspect_tools("wav")


def fixture(path, frequency=440, rate=48000, channels=1, duration=2):
    args = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate={rate}:duration={duration}",
            "-ac", str(channels)]
    if path.suffix == ".wav":
        args += ["-c:a", "pcm_s24le"]
    elif path.suffix == ".mp3":
        args += ["-c:a", "libmp3lame", "-q:a", "2"]
    elif path.suffix == ".m4a":
        args += ["-c:a", "aac", "-b:a", "128k"]
    subprocess.run(args + [str(path)], check=True, capture_output=True, timeout=30,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return FileRecord.from_path(path)


def make(records, **options):
    return plan_audio(records, PlanRequest("audio", extension=options.pop("extension", "wav"), **options))


def run(tmp_path, plan, **options):
    store = Store(tmp_path / "history")
    try:
        job = store.enqueue("Edit audio", "audio", audio_snapshot(plan))
        execute_job(store, job, **options)
        return store.items(job)
    finally:
        store.close()


def decoded(path, tools, rate=48000):
    return array.array("h", subprocess.check_output([tools["ffmpeg"], "-v", "error", "-i", str(path),
        "-map", "0:a:0", "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))


@pytest.mark.parametrize("start,end", [("0.25", "1.25"), ("00:00.2", "00:01.7"), ("1.99", "2")])
def test_sample_accurate_trim(tmp_path, tools, start, end):
    source = fixture(tmp_path / "音声 [1].wav")
    before = Path(source.path).read_bytes()
    plan = make([source], audio_action="trim", audio_start=start, audio_end=end)
    assert plan.counts == {"Planned": 1}, plan.items
    results = run(tmp_path, plan)
    assert results[0]["state"] == "succeeded", results
    samples = decoded(plan.items[0].output, tools)
    original = decoded(source.path, tools)
    assert samples == original[round(seconds(start)*48000):round(seconds(end)*48000)]
    assert Path(source.path).read_bytes() == before


def test_split_final_segment_and_fades(tmp_path, tools):
    source = fixture(tmp_path / "song.wav")
    plan = make([source], audio_action="split", audio_ranges="0 - 0.5\n0.75 - 1.25\n1.5 - end", audio_fade_in=.1, audio_fade_out=.1)
    assert plan.counts == {"Planned": 3}, plan.items
    assert [Path(i.output).name for i in plan.items] == ["song_part01.wav", "song_part02.wav", "song_part03.wav"]
    results = run(tmp_path, plan)
    assert all(i["state"] == "succeeded" for i in results), results
    for item in plan.items:
        samples = decoded(item.output, tools)
        assert len(samples) == 24000
        edge = sum(abs(v) for v in samples[:480]) / 480
        middle = sum(abs(v) for v in samples[10000:11000]) / 1000
        assert edge < middle * .15
        assert sum(abs(v) for v in samples[-480:])/480 < middle * .15


def test_join_order_and_common_encoding_preset(tmp_path, tools):
    first = fixture(tmp_path / "first.wav", frequency=440, rate=44100)
    second = fixture(tmp_path / "second.wav", frequency=880, rate=48000, channels=2)
    with pytest.raises(ValueError, match="common"):
        make([first, second], audio_action="join", audio_tracks_mode="single")
    plan = make([first, second], audio_action="join", audio_tracks_mode="single", audio_sample_rate=48000,
                audio_channels="stereo", audio_join_order=(second.path, first.path), audio_fade_out=.2)
    assert plan.counts == {"Planned": 1}
    assert [s["source"] for s in plan.items[0].audio_spec["sources"]] == [second.path, first.path]
    results = run(tmp_path, plan)
    assert results[0]["state"] == "succeeded", results
    samples = decoded(plan.items[0].output, tools)
    assert abs(len(samples) - 192000) <= 2
    def hz(chunk):
        return sum(a < 0 <= b for a,b in zip(chunk, chunk[1:])) * 2
    assert 870 < hz(samples[12000:36000]) < 890
    assert 430 < hz(samples[108000:132000]) < 450


@pytest.mark.parametrize("extension", ["mp3", "m4a"])
def test_copy_trim_keeps_previewed_packet_payloads(tmp_path, tools, extension):
    source = fixture(tmp_path / f"copy.{extension}")
    plan = make([source], extension=extension, audio_mode="copy", audio_action="trim", audio_start="0.3", audio_end="1.1")
    assert plan.counts == {"Planned": 1}, plan.items
    assert "packet boundaries" in plan.items[0].reason
    results = run(tmp_path, plan)
    assert results[0]["state"] == "succeeded", results
    assert abs(len(decoded(plan.items[0].output, tools)) / 48000 - .8) < .1


@pytest.mark.parametrize("extension", ["mp3", "m4a"])
def test_copy_join_order_payloads_unicode_and_quotes(tmp_path, tools, extension):
    first = fixture(tmp_path / f"first ' 日本.{extension}")
    second = fixture(tmp_path / f"second.{extension}", frequency=880)
    plan = make([first, second], extension=extension, audio_mode="copy", audio_action="join", audio_tracks_mode="single",
                audio_join_order=(second.path, first.path))
    results = run(tmp_path, plan)
    assert results[0]["state"] == "succeeded", results
    assert len(decoded(plan.items[0].output, tools)) / 48000 > 3.9


@pytest.mark.parametrize("options", [
    dict(audio_action="trim", audio_start="2", audio_end="1"),
    dict(audio_action="trim", audio_start="0", audio_end="3"),
    dict(audio_action="trim", audio_start="-1"),
    dict(audio_action="trim", audio_start="NaN"),
    dict(audio_action="split", audio_ranges="0 - 1\n0.5 - end"),
    dict(audio_action="split", audio_ranges="0 - 0"),
    dict(audio_action="split", audio_ranges="0 - end\n1 - end"),
    dict(audio_action="split", audio_ranges=""),
    dict(audio_action="trim", audio_fade_in=1.5, audio_fade_out=1),
])
def test_invalid_ranges_and_fades_block_all_outputs(tmp_path, tools, options):
    source = fixture(tmp_path / "source.wav")
    try:
        plan = make([source], **options)
    except ValueError:
        return
    assert any(i.status == "Invalid" for i in plan.items), plan.items
    with pytest.raises(ValueError):
        audio_snapshot(plan)
    assert len(list(tmp_path.glob("*.wav"))) == 1


def test_join_source_change_and_late_collision_are_safe(tmp_path, tools):
    first = fixture(tmp_path / "first.wav")
    second = fixture(tmp_path / "second.wav", frequency=880)
    plan = make([first, second], audio_action="join", audio_tracks_mode="single")
    output = Path(plan.items[0].output)
    output.write_bytes(b"late arrival")
    assert run(tmp_path, plan)[0]["state"] == "failed"
    assert output.read_bytes() == b"late arrival"
    plan = make([first, second], audio_action="join", audio_tracks_mode="single")
    with Path(second.path).open("ab") as handle:
        handle.write(b"changed")
    assert run(tmp_path, plan)[0]["state"] == "failed"
    assert not Path(plan.items[0].output).exists()


def test_join_rejects_stale_order_and_copy_fades(tmp_path, tools):
    first = fixture(tmp_path / "first.mp3")
    second = fixture(tmp_path / "second.mp3")
    with pytest.raises(ValueError, match="inputs changed"):
        make([first, second], extension="mp3", audio_action="join", audio_join_order=(first.path,), audio_tracks_mode="single")
    with pytest.raises(ValueError, match="Fades require"):
        make([first, second], extension="mp3", audio_action="join", audio_mode="copy", audio_fade_in=.1, audio_tracks_mode="single")


def test_join_removes_container_end_padding(tmp_path, tools):
    first = fixture(tmp_path / "first.m4a", duration=.7)
    second = fixture(tmp_path / "second.mp3", duration=.9, frequency=880)
    plan = make([first, second], audio_action="join", audio_tracks_mode="single")
    results = run(tmp_path, plan)
    assert results[0]["state"] == "succeeded", results
    samples = decoded(plan.items[0].output, tools)
    assert abs(len(samples)/48000 - plan.items[0].audio_spec["duration"]) <= 1/48000
    assert abs(len(samples)/48000 - 1.6) < .05


def test_split_cancel_retry(tmp_path, tools):
    source = fixture(tmp_path / "first.wav")
    plan = make([source], audio_action="split", audio_ranges="0 - 1\n1 - end")
    store = Store(tmp_path / "history")
    try:
        job = store.enqueue("Split", "audio", audio_snapshot(plan))
        cancelled = False
        def stop(point):
            nonlocal cancelled
            if point == "progress":
                cancelled = True
        execute_job(store, job, cancelled=lambda: cancelled, hook=stop)
        assert all(i["state"] == "cancelled" for i in store.items(job))
        assert not list(tmp_path.glob("*.part"))
        assert store.retry(job) == 2
        execute_job(store, job)
        assert all(i["state"] == "succeeded" for i in store.items(job))
    finally:
        store.close()


def test_ui_tasks_order_dialog_and_join_results(window, tmp_path, tools):
    from PySide6.QtCore import QTimer, Qt
    from PySide6.QtWidgets import QApplication, QListWidget
    first = fixture(tmp_path / "first.wav")
    second = fixture(tmp_path / "second.wav", frequency=880)
    window._tool_changed("Audio")
    window.format.setCurrentText("WAV")
    window.model.append([first, second])
    controls = window.audio_controls
    controls.task.setCurrentIndex(1)
    assert controls.trim_panel.isVisible() and not controls.split_panel.isVisible()
    controls.task.setCurrentIndex(2)
    assert controls.split_panel.isVisible() and not controls.trim_panel.isVisible()
    controls.task.setCurrentIndex(3)
    assert controls.order_button.isVisible() and controls.tracks.currentData() == "single"
    def accept():
        dialog = QApplication.activeModalWidget()
        listing = dialog.findChild(QListWidget)
        item = listing.takeItem(1)
        listing.insertItem(0, item)
        dialog.accept()
    QTimer.singleShot(20, accept)
    controls.order_button.click()
    assert controls.join_order == (second.path, first.path)
    queue = window.ensure_jobs()
    window.start_rename_button.click()
    wait_until(lambda: window.plan_worker is None and queue.worker is None and window.tabs.currentIndex() == 2, 40000)
    assert window.result_model.items[0]["state"] == "succeeded", window.status.text()
    assert window.model.rowCount() == 0
    assert Path(first.path).exists() and Path(second.path).exists()


@pytest.mark.parametrize("extension", ["mp3", "m4a", "flac", "wav", "opus"])
def test_encoded_trim_all_output_formats(tmp_path, tools, extension):
    source = fixture(tmp_path / "source.wav")
    plan = make([source], extension=extension, audio_action="trim", audio_start="0.4", audio_end="1.4", audio_fade_in=.1, audio_fade_out=.1)
    result = run(tmp_path, plan)
    assert result[0]["state"] == "succeeded", result
    assert abs(len(decoded(plan.items[0].output, tools)) / 48000 - 1) < .1


@pytest.mark.parametrize("point", ["created", "prepared", "committed"])
def test_actual_join_process_loss_recovers(tmp_path, tools, point):
    first = fixture(tmp_path / "first.wav")
    second = fixture(tmp_path / "second.wav", frequency=880)
    before = [hashlib.sha256(Path(s.path).read_bytes()).hexdigest() for s in (first, second)]
    plan = make([first, second], audio_action="join", audio_tracks_mode="single")
    store = Store(tmp_path / "history")
    try:
        job = store.enqueue("Join", "audio", audio_snapshot(plan))
        code = """import os,sys
from media_tool.jobs import Store,execute_job
store=Store(sys.argv[1])
def hook(point):
    if point==sys.argv[3]: os._exit(23)
execute_job(store,sys.argv[2],hook=hook)
"""
        child = subprocess.run([sys.executable, "-c", code, str(store.directory), job, point],
            env=dict(os.environ, PYTHONPATH=str(Path("src").resolve())), timeout=40,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        assert child.returncode == 23
        store.recover()
        assert store.retry(job) == 1
        execute_job(store, job)
        assert store.items(job)[0]["state"] == "succeeded", store.items(job)
        assert len(list(tmp_path.glob("*_joined.wav"))) == 1
        assert not list(tmp_path.glob("*.part"))
        assert [hashlib.sha256(Path(s.path).read_bytes()).hexdigest() for s in (first, second)] == before
    finally:
        store.close()


def test_later_join_input_changes_at_commit(tmp_path, tools):
    first = fixture(tmp_path / "first.wav")
    second = fixture(tmp_path / "second.wav")
    plan = make([first, second], audio_action="join", audio_tracks_mode="single")
    def hook(point):
        if point == "prepared":
            with Path(second.path).open("ab") as handle:
                handle.write(b"external change")
    results = run(tmp_path, plan, hook=hook)
    assert results[0]["state"] == "failed", results
    assert not Path(plan.items[0].output).exists() and not list(tmp_path.glob("*.part"))


def test_copy_join_rejects_incompatible_parameters(tmp_path, tools):
    first = fixture(tmp_path / "first.m4a", rate=44100)
    second = fixture(tmp_path / "second.m4a", rate=48000)
    with pytest.raises(ValueError, match="common|matching"):
        make([first, second], extension="m4a", audio_action="join", audio_mode="copy", audio_tracks_mode="single")


def test_copy_split_snaps_each_preview_range(tmp_path, tools):
    source = fixture(tmp_path / "source.m4a")
    plan = make([source], extension="m4a", audio_action="split", audio_mode="copy", audio_ranges="0.2 - 0.8\n1 - 1.8")
    assert plan.counts == {"Planned": 2}, plan.items
    assert all(i.audio_spec["edit"]["copy_packets"]["count"] > 0 for i in plan.items)
    results = run(tmp_path, plan)
    assert all(i["state"] == "succeeded" for i in results), results


def test_trim_video_multiple_tracks_and_join_ambiguous_selection(tmp_path, tools):
    from test_audio import multiple
    source = multiple(tmp_path / "video.mkv")
    preview = make([source], audio_action="trim", audio_start="0.1", audio_end="0.6", audio_tracks_mode="custom", audio_tracks="2")
    assert "_track02_trim.wav" in preview.items[0].output
    assert run(tmp_path, preview)[0]["state"] == "succeeded"
    other = fixture(tmp_path / "other.wav")
    with pytest.raises(ValueError, match="exactly one"):
        make([source, other], audio_action="join", audio_tracks_mode="all")


def test_schema18_saved_audio_job_runs_after_upgrade(tmp_path, tools):
    source = fixture(tmp_path / "source.wav")
    plan = make([source], extension="flac")
    store = Store(tmp_path / "history")
    job = store.enqueue("Previous Audio", "audio", audio_snapshot(plan))
    store.db.execute("PRAGMA user_version=18")
    store.close()
    store = Store(tmp_path / "history")
    try:
        from media_tool.jobs import SCHEMA
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA
        execute_job(store, job)
        assert store.items(job)[0]["state"] == "succeeded"
    finally:
        store.close()


def test_supervised_manifest_unicode_and_cancel_blocked_reader(tmp_path):
    from media_tool.processes import run_process
    from media_tool.jobs import Cancelled
    text = "日本 ' quoted \nsecond line"
    assert run_process([sys.executable, "-c", "import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())"], input_text=text) == text
    marker = tmp_path / "child.pid"
    child_code = "import os,sys,time;from pathlib import Path;p=Path(sys.argv[1]);q=p.with_suffix('.new');q.write_text(str(os.getpid()));q.replace(p);time.sleep(60)"
    with pytest.raises(Cancelled):
        run_process([sys._base_executable, "-c", child_code, str(marker)], input_text="x"*65536, cancelled=marker.exists)
    import psutil
    pid = int(marker.read_text())
    try:
        psutil.Process(pid).wait(timeout=5)
    except psutil.NoSuchProcess:
        pass
    assert not psutil.pid_exists(pid)


def test_manifest_child_stops_when_parent_dies(tmp_path):
    import psutil
    marker = tmp_path / "child.pid"
    driver_code = """import os,sys,time,threading
from pathlib import Path
from media_tool.processes import run_process
marker=Path(sys.argv[1])
def disappear():
    while not marker.exists(): time.sleep(.01)
    os._exit(17)
threading.Thread(target=disappear,daemon=True).start()
code="import os,sys,time;from pathlib import Path;p=Path(sys.argv[1]);q=p.with_suffix('.new');q.write_text(str(os.getpid()));q.replace(p);time.sleep(60)"
run_process([sys._base_executable,'-c',code,str(marker)],input_text='x'*65536)
"""
    result = subprocess.run([sys.executable, "-c", driver_code, str(marker)], timeout=20,
        env=dict(os.environ, PYTHONPATH=str(Path("src").resolve())),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 17
    pid = int(marker.read_text())
    try:
        psutil.Process(pid).wait(timeout=5)
    except psutil.NoSuchProcess:
        pass
    assert not psutil.pid_exists(pid)
