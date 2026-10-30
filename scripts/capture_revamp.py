"""Capture the real Qt workspace for visual review; no application package build."""
import os
from pathlib import Path
import sys
import argparse
import json
import subprocess
import time

os.environ['QT_QPA_PLATFORM']='windows'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from PySide6.QtCore import QSettings
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from media_tool.window import Workspace
from media_tool.domain import FileRecord

parser=argparse.ArgumentParser()
parser.add_argument('--live',action='store_true')
args=parser.parse_args()

def wait_until(predicate,timeout=60):
    deadline=time.monotonic()+timeout
    while not predicate():
        if time.monotonic()>deadline:raise RuntimeError('Native UI check timed out')
        QTest.qWait(10)
        time.sleep(.001)

root=Path(__file__).resolve().parents[1]
out=root/'artifacts'/'revamp-captures'
out.mkdir(parents=True,exist_ok=True)
app=QApplication([])
settings=QSettings(str(out/'review.ini'),QSettings.Format.IniFormat)
settings.clear()
w=Workspace(settings)
w.show()
for width,height in ((1180,740),(880,610)):
    w.resize(width,height)
    for tool in ('Images','Video','Audio','Rename','Archives','Documents','Folders','Queue'):
        if tool=='Folders':w.show_folders()
        elif tool=='Queue':w.open_jobs()
        else:w._tool_changed(tool)
        QTest.qWait(120)
        w.grab().save(str(out/f'{tool.lower()}-{width}.png'))
if args.live:
    from media_tool.tool_paths import resolve_tool
    fixtures=out/'live-fixtures'
    fixtures.mkdir(exist_ok=True)
    ffmpeg=resolve_tool('ffmpeg')
    movie=fixtures/'Evening walk.mkv'
    sound=fixtures/'Morning session.wav'
    def create(path,options):
        if not path.exists():subprocess.run([ffmpeg,'-hide_banner','-nostdin','-v','error',*options,str(path)],
            check=True,capture_output=True,timeout=60,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    create(movie,['-f','lavfi','-i','testsrc2=size=640x360:rate=24','-t','18','-c:v','libx264','-preset','ultrafast','-threads','1'])
    create(sound,['-f','lavfi','-i','sine=frequency=440:duration=6','-af','tremolo=f=2:d=0.8,afade=t=in:d=0.4,afade=t=out:st=5:d=1','-c:a','pcm_s16le'])
    w._tool_changed('Video')
    w.model.replace_order([FileRecord.from_path(movie)])
    w.destination.setText(str(fixtures))
    w.start_rename_button.click()
    wait_until(lambda:w.jobs_dialog.worker is not None)
    wait_until(lambda:any(e.get('processed',0)>0 for e in w.visual.activity.events.values()))
    for width,height in ((1180,740),(880,610)):
        w.resize(width,height)
        w.visual.activity.tick()
        QTest.qWait(40)
        w.grab().save(str(out/f'video-results-{width}.png'))
    wait_until(lambda:w.jobs_dialog.worker is None)
    results=w.jobs_dialog.store.items(w.last_result_job)
    assert all(i['state']=='succeeded' for i in results),results
    assert movie.exists()
    w.resize(1180,740)
    w.grab().save(str(out/'video-completed.png'))
    w._tool_changed('Audio')
    w.model.replace_order([FileRecord.from_path(sound)])
    controls=w.audio_controls
    controls.task.setCurrentIndex(controls.task.findData('trim'))
    controls.start.setText('1')
    controls.end.setText('4.5')
    w.preview_button.click()
    wait_until(lambda:w.plan_worker is None and w.tabs.currentIndex()==1)
    wait_until(lambda:w.waveform.canvas.data is not None)
    wait_until(lambda:w.waveform.worker is None)
    for theme in ('dark','light'):
        w.set_theme(theme,persist=False)
        for width,height in ((1180,740),(880,610)):
            w.resize(width,height)
            QTest.qWait(80)
            w.grab().save(str(out/f'audio-waveform-{theme}-{width}.png'))
    (out/'native-check.json').write_text(json.dumps(dict(platform=app.platformName(),
        real_video_conversion='succeeded',waveform_bins=len(w.waveform.canvas.data['peaks']),
        cached_tracks=len(w.waveform.cache),originals_present=movie.exists() and sound.exists()),indent=2))
w.close()
wait_until(lambda:not w.isVisible())
QTest.qWait(200)
print(out)
