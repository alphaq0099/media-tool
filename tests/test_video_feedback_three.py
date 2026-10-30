"""Regressions for expanded settings, elapsed display and long audio tails."""
import copy
import time
from types import SimpleNamespace
from pathlib import Path
import subprocess

import pytest
from PySide6.QtTest import QTest
from test_foundation import app, window
from test_video import media_tools, run_plan
from media_tool.domain import FileRecord
from media_tool.planning import PlanRequest
from media_tool.video import plan_video, probe, packet_durations, verify_output


def test_expanded_settings_do_not_distribute_blank_space(window):
    window._tool_changed('Video')
    r=window.resize_controls
    c=window.compression_controls
    r.mode.setCurrentIndex(r.mode.findData('dimensions'))
    for mode in ('quality','target_size','quality'):
        c.mode.setCurrentIndex(c.mode.findData(mode))
        window.resize(1180,950)
        QTest.qWait(30)
        for panel in (r.panel,c.panel,c.target_panel,window.visual.quality_pair):
            if not panel.isVisible():continue
            def check(layout):
                items=[layout.itemAt(i) for i in range(layout.count()) if not layout.itemAt(i).isEmpty()]
                for a,b in zip(items,items[1:]):
                    if b.geometry().top()>a.geometry().top():
                        assert b.geometry().top()-a.geometry().bottom()<=16
                for item in items:
                    if item.layout():check(item.layout())
                    elif item.widget() and item.widget().layout():check(item.widget().layout())
            check(panel.layout())


def test_elapsed_cell_advances_without_history_change_or_selection_reset(window):
    q=window.ensure_jobs()
    job=q.store.enqueue('Long operation','sample',[dict(name='file',seconds=0)])
    q.refresh(job)
    q.run_elapsed_base=60
    q.worker=SimpleNamespace(job_id=job,started_at=time.monotonic()-65)
    try:
        selected=q.jobs_table.currentIndex().row()
        q.update_live_elapsed()
        assert q.jobs_model.rows[q.jobs_model.keys.index(job)][3]=='2:05'
        assert q.jobs_table.currentIndex().row()==selected
    finally:q.worker=None


@pytest.mark.parametrize('codec,target',[('h264',False),('hevc',True)])
def test_mkv_video_duration_is_not_extended_to_audio_tail(tmp_path,media_tools,codec,target):
    source=tmp_path/'tail.mp4'
    subprocess.run([media_tools['ffmpeg'],'-v','error','-f','lavfi','-i','testsrc2=size=160x96:rate=24:duration=2',
        '-f','lavfi','-i','sine=sample_rate=44100:duration=2.6','-c:v','libx264','-preset','ultrafast','-threads','1',
        '-c:a','aac',str(source)],check=True,capture_output=True,timeout=30,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    before=source.read_bytes()
    plan=plan_video([FileRecord.from_path(source)],PlanRequest('video',extension='mkv',video_codec=codec,
        video_mode='target_size' if target else 'quality',video_target_size=1,video_target_unit='MiB',video_audio_mode='all'))
    rows=run_plan(tmp_path,plan)
    assert rows[0]['state']=='succeeded',rows
    path=Path(plan.items[0].output)
    info=probe(path,media_tools,track_details=True)
    assert info['duration']>2.5 and info['streams'][0]['duration'] is None
    timed=packet_durations(path,info,media_tools,lambda:False)
    assert abs(timed['streams'][0]['duration']-2)<.05
    verify_output(plan.items[0].video_spec,timed)
    shortened=copy.deepcopy(timed)
    shortened['streams'][0]['duration']=1
    with pytest.raises(ValueError,match='video duration'):verify_output(plan.items[0].video_spec,shortened)
    assert source.read_bytes()==before
    if codec=='h264':
        truncated=tmp_path/'truncated.mkv'
        subprocess.run([media_tools['ffmpeg'],'-v','error','-i',str(source),'-vf','trim=duration=1',
            '-c:v','libx264','-preset','ultrafast','-threads','1','-c:a','aac',str(truncated)],
            check=True,capture_output=True,timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        bad=packet_durations(truncated,probe(truncated,media_tools,track_details=True),media_tools,lambda:False)
        assert bad['duration']>2.5
        with pytest.raises(ValueError,match='video duration'):verify_output(plan.items[0].video_spec,bad)
