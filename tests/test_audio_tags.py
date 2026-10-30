from pathlib import Path
import pytest
from PIL import Image
from media_tool.audio import plan_audio
from media_tool.planning import PlanRequest
from media_tool.audio_tags import read_tags, cover_info
from media_tool.domain import FileRecord
from test_audio import tone, plan, run as run_audio
from test_audio_levels import run
from test_foundation import app, window


@pytest.mark.parametrize("extension", ["mp3", "m4a", "flac", "wav", "opus"])
def test_unicode_tags_cover_and_encoded_payload_preservation(tmp_path, extension):
    source = tone(tmp_path / "source.wav")
    initial = plan([source], extension)
    assert run_audio(tmp_path, initial)[0]["state"] == "succeeded"
    source = FileRecord.from_path(initial.items[0].output)
    original = Path(source.path).read_bytes()
    cover = tmp_path / "cover.png"
    Image.new("RGB", (30,20), "red").save(cover)
    req = PlanRequest("audio", extension=extension, audio_action="tags", audio_tag_mode="edit",
        audio_tag_title="日本 café", audio_tag_artist="Artist", audio_tag_album="Album", audio_tag_track="3/12",
        audio_tag_date="2026", audio_tag_genre="Rock", audio_cover_mode="replace", audio_cover_path=str(cover))
    preview = plan_audio([source], req)
    assert preview.counts == {"Planned": 1}, preview.items
    rows = run(tmp_path, preview)
    assert rows[0]["state"] == "succeeded", rows
    values, pictures = read_tags(preview.items[0].output)
    assert values == dict(title="日本 café", artist="Artist", album="Album", track="3/12", date="2026", genre="Rock")
    assert pictures == [cover.read_bytes()]
    assert Path(source.path).read_bytes() == original
    tagged = FileRecord.from_path(preview.items[0].output)
    kept = plan_audio([tagged], PlanRequest("audio", extension=extension, audio_action="tags"))
    assert run(tmp_path, kept)[0]["state"] == "succeeded"
    assert read_tags(kept.items[0].output) == (values,pictures)
    cleared = plan_audio([tagged], PlanRequest("audio", extension=extension, audio_action="tags", audio_tag_mode="remove", audio_cover_mode="remove"))
    assert run(tmp_path, cleared)[0]["state"] == "succeeded"
    assert all(not v for v in read_tags(cleared.items[0].output)[0].values())
    assert not read_tags(cleared.items[0].output)[1]


def test_changed_cover_blocks_output(tmp_path):
    source = tone(tmp_path / "source.wav")
    cover = tmp_path / "cover.png"
    Image.new("RGB", (10,10), "red").save(cover)
    preview = plan_audio([source], PlanRequest("audio", extension="wav", audio_action="tags", audio_cover_mode="replace", audio_cover_path=str(cover)))
    Image.new("RGB", (10,10), "blue").save(cover)
    rows = run(tmp_path, preview)
    assert rows[0]["state"] == "failed"
    assert not Path(preview.items[0].output).exists()


def test_tags_ui_uses_copy_and_supported_policy(window):
    window._tool_changed("Audio")
    controls = window.audio_controls
    controls.task.setCurrentIndex(controls.task.findData("tags"))
    assert controls.tag_panel.isVisible() and controls.mode.currentData() == "copy"
    controls.tag_mode.setCurrentIndex(1)
    assert controls.tag_edit.isVisible()
    controls.cover_mode.setCurrentIndex(1)
    assert controls.cover_path.isVisible()


@pytest.mark.parametrize('settings',[{'audio_tag_date':'2026-13-01'},{'audio_tag_track':'99999'}])
def test_unportable_tag_values_fail_before_work(settings):
    with pytest.raises(ValueError,match='date|65535'):
        plan_audio([],PlanRequest('audio',extension='m4a',audio_action='tags',audio_tag_mode='edit',**settings))


def test_tag_writer_checks_the_open_file_identity(tmp_path):
    from media_tool.audio_tags import apply_tags
    path=tmp_path/'unrelated.txt'
    path.write_bytes(b'Unrelated content')
    with pytest.raises(ValueError,match='replaced before tag writing'):
        apply_tags({'tags':{'cover':None}},path,lambda:False,('0','0'))
    assert path.read_bytes()==b'Unrelated content'
