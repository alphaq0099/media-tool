from pathlib import Path
import math
import pytest
from media_tool.planning import PlanRequest
from media_tool.audio import plan_audio, audio_snapshot
from media_tool.audio_levels import measure, finite
from media_tool.jobs import Store, execute_job
from test_audio_edit import fixture, decoded
from test_foundation import app, window, wait_until


def run(tmp_path, plan):
    store = Store(tmp_path / "history")
    try:
        job = store.enqueue("Levels", "audio", audio_snapshot(plan))
        execute_job(store, job)
        return store.items(job)
    finally:
        store.close()


@pytest.mark.parametrize("extension", ["wav", "mp3", "m4a", "flac", "opus"])
def test_actual_normalization_and_output_measurement(tmp_path, extension):
    source = fixture(tmp_path / "source.wav", duration=3)
    before = Path(source.path).read_bytes()
    plan = plan_audio([source], PlanRequest("audio", extension=extension, audio_action="normalize", audio_lufs=-18))
    assert plan.counts == {"Planned": 1}, plan.items
    rows = run(tmp_path, plan)
    assert rows[0]["state"] == "succeeded", rows
    measured = measure(plan.items[0].audio_spec, plan.items[0].output)
    assert abs(float(measured["input_i"]) + 18) <= .5
    assert float(measured["input_tp"]) <= -1.35
    assert "Measured output" in rows[0]["message"]
    assert Path(source.path).read_bytes() == before


def test_gain_and_clipping_rejection(tmp_path):
    source = fixture(tmp_path / "source.wav", duration=3)
    plan = plan_audio([source], PlanRequest("audio", extension="wav", audio_action="volume", audio_gain=6))
    rows = run(tmp_path, plan)
    assert rows[0]["state"] == "succeeded", rows
    spec = plan.items[0].audio_spec
    actual = measure(spec, spec["output"])
    assert abs(float(actual["input_tp"]) - float(spec["levels"]["input"]["input_tp"]) - 6) < .1
    invalid = plan_audio([source], PlanRequest("audio", extension="wav", audio_action="volume", audio_gain=24))
    assert invalid.items[0].status == "Invalid" and "clip" in invalid.items[0].reason


def test_silence_is_not_falsely_normalized(tmp_path):
    import subprocess, os
    from media_tool.domain import FileRecord
    path = tmp_path / "silence.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "3", str(path)],
        check=True, capture_output=True, creationflags=getattr(os, "CREATE_NO_WINDOW", 0))
    plan = plan_audio([FileRecord.from_path(path)], PlanRequest("audio", extension="wav", audio_action="normalize"))
    assert plan.items[0].status == "Invalid" and "silent" in plan.items[0].reason
    with pytest.raises(ValueError):
        audio_snapshot(plan)


def test_copy_processing_blocked_and_ui_forces_encode(window):
    controls = window.audio_controls
    window._tool_changed("Audio")
    controls.mode.setCurrentIndex(1)
    controls.task.setCurrentIndex(controls.task.findData("normalize"))
    assert controls.mode.currentData() == "encode"
    assert controls.level_panel.isVisible() and not controls.fade_panel.isVisible()
    assert controls.lufs.isVisible() and not controls.gain.isVisible()
