import hashlib
from pathlib import Path
import threading
import time

import pytest
from PySide6.QtCore import Qt,QPoint
from PySide6.QtTest import QTest

from media_tool.waveform_data import extract
from test_audio import tone,multiple,ffmpeg
from test_foundation import app,window,wait_until


def test_real_waveform_is_bounded_and_preserves_source(tmp_path):
    record=tone(tmp_path/'tone.wav',duration=3)
    before=hashlib.sha256(Path(record.path).read_bytes()).digest()
    result=extract(record)
    assert result['duration']==pytest.approx(3,abs=.01)
    assert len(result['peaks'])==1200
    assert max(result['peaks'])>.05
    assert all(0<=v<=1 for v in result['peaks'])
    assert hashlib.sha256(Path(record.path).read_bytes()).digest()==before
    assert Path(record.path).stat().st_mtime_ns==record.modified_ns


def test_real_waveform_selects_actual_audio_stream(tmp_path):
    record=multiple(tmp_path/'tracks.mkv')
    result=extract(record,1)
    assert result['track']==1
    assert len(result['tracks'])==2
    assert result['tracks'][1]['index']==2
    with pytest.raises(ValueError,match='unavailable'):extract(record,2)


def test_delayed_short_track_uses_its_own_time_origin_and_duration(tmp_path):
    from media_tool.domain import FileRecord
    path=tmp_path/'delayed.mkv'
    ffmpeg(['-f','lavfi','-i','color=size=32x32:rate=10:duration=4',
        '-itsoffset','1','-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=0.8',
        '-map','0:v','-map','1:a','-c:v','libx264','-threads','1','-c:a','pcm_s16le',path])
    result=extract(FileRecord.from_path(path))
    assert result['duration']==pytest.approx(.8,abs=.03)
    assert max(result['peaks'][:50])>.05
    assert max(result['peaks'][-50:])>.05


def test_waveform_trim_and_split_fields_and_navigation(window,tmp_path):
    record=tone(tmp_path/'tone.wav',duration=5)
    window._tool_changed('Audio')
    window.model.append([record])
    controls=window.audio_controls
    controls.task.setCurrentIndex(controls.task.findData('trim'))
    controls.start.setText('1')
    controls.end.setText('4')
    window.tabs.setCurrentIndex(1)
    wait_until(lambda:window.waveform.canvas.data is not None,15000)
    canvas=window.waveform.canvas
    assert canvas.ranges==[(1,4)]
    assert len(window.waveform.cache)==1
    assert window.preview_table.isVisible()
    canvas.handle='end'
    canvas.setFocus()
    QTest.keyClick(canvas,Qt.Key.Key_Left)
    assert canvas.ranges[0][1]==pytest.approx(3.99)
    QTest.mousePress(canvas,Qt.MouseButton.LeftButton,pos=QPoint(int(canvas.x(1)),30))
    QTest.mouseMove(canvas,QPoint(int(canvas.x(2)),30))
    QTest.mouseRelease(canvas,Qt.MouseButton.LeftButton,pos=QPoint(int(canvas.x(2)),30))
    assert canvas.ranges[0][0]==pytest.approx(2,abs=.03)
    controls.task.setCurrentIndex(controls.task.findData('split'))
    controls.ranges.setPlainText('0 - 2\n3 - 5')
    assert canvas.ranges==[(0,2),(3,5)]
    canvas.selected=0
    canvas.handle='end'
    canvas.move_handle(4)
    assert canvas.ranges==[(0,3),(3,5)]
    assert '00:03.000' in controls.ranges.toPlainText()
    controls.task.setCurrentIndex(controls.task.findData('trim'))
    window._tool_changed('Images')
    assert not window.waveform.isVisible()
    window._tool_changed('Audio')
    assert controls.task.currentData()=='trim'
    assert window.tabs.currentIndex()==0
    window.tabs.setCurrentIndex(1)
    wait_until(lambda:window.waveform.worker is None)
    assert len(window.waveform.cache)==1
    for control,value in zip(window.waveform.selection_fields,('1.5','3')):control.setText(value)
    window.waveform.edit_selection()
    assert canvas.ranges==[(1.5,3)]
    controls.task.setCurrentIndex(controls.task.findData('convert'))
    assert not window.waveform.isVisible()


def test_start_cancels_preview_analysis_before_running_real_trim(window,tmp_path,monkeypatch):
    from media_tool import waveform_data
    from media_tool.jobs import Cancelled
    import json
    started=threading.Event()
    cancelled=threading.Event()
    def paused_extract(record,track,check):
        started.set()
        deadline=time.monotonic()+10
        while not check() and time.monotonic()<deadline:time.sleep(.01)
        cancelled.set()
        raise Cancelled()
    monkeypatch.setattr(waveform_data,'extract',paused_extract)
    source=tone(tmp_path/'trim.wav',duration=2)
    before=Path(source.path).read_bytes()
    window._tool_changed('Audio')
    window.model.append([source])
    controls=window.audio_controls
    controls.task.setCurrentIndex(controls.task.findData('trim'))
    controls.end.setText('0.5')
    window.preview_changes()
    wait_until(lambda:window.plan_worker is None and started.is_set(),15000)
    window.start_rename_button.click()
    queue=window.jobs_dialog
    wait_until(cancelled.is_set)
    wait_until(lambda:queue.worker is not None)
    assert window.waveform.worker is None
    assert window.tabs.currentIndex()==2
    wait_until(lambda:queue.worker is None,15000)
    item=queue.store.items(window.last_result_job)[0]
    assert item['state']=='succeeded',item['message']
    assert Path(json.loads(item['spec'])['output']).is_file()
    assert Path(source.path).read_bytes()==before


def test_cached_waveform_rejects_changed_source(window,tmp_path):
    source=tone(tmp_path/'changed.wav',duration=1)
    window._tool_changed('Audio')
    window.model.append([source])
    window.audio_controls.task.setCurrentIndex(window.audio_controls.task.findData('trim'))
    window.tabs.setCurrentIndex(1)
    wait_until(lambda:window.waveform.canvas.data is not None,15000)
    wait_until(lambda:window.waveform.worker is None)
    Path(source.path).write_bytes(b'changed source')
    window.waveform.refresh()
    assert window.waveform.canvas.data is None
    assert 'Source changed' in window.waveform.message.text()
    assert not window.waveform.canvas.isEnabled()
