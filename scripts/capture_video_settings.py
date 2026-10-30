"""Capture expanded Video settings with an isolated synthetic 4K input."""
import os
from pathlib import Path
import subprocess
import sys
import time

root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'src'))
os.environ.setdefault('QT_QPA_PLATFORM','windows' if os.name=='nt' else 'offscreen')
from PySide6.QtCore import QSettings,Qt
from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from media_tool.window import Workspace
from media_tool.tool_paths import resolve_tool

out=root/'artifacts'/'video-settings-captures'
out.mkdir(parents=True,exist_ok=True)
source=out/'Layout sample.mp4'
subprocess.run([resolve_tool('ffmpeg'),'-v','error','-y','-f','lavfi','-i',
    'color=c=0x5bd4bf:size=3840x2160:rate=1','-frames:v','1','-c:v','libx264',
    '-preset','ultrafast','-threads','1',str(source)],check=True,
    capture_output=True,timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
app=QApplication([])
app.setStyle('Fusion')
app.setFont(QFont('Segoe UI' if os.name=='nt' else 'Sans Serif',10))
settings=QSettings(str(out/'settings.ini'),QSettings.IniFormat)
settings.clear()
w=Workspace(settings)
w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen,True)
w.show()
w._tool_changed('Video')
w.resize_controls.mode.setCurrentIndex(w.resize_controls.mode.findData('dimensions'))
w.compression_controls.mode.setCurrentIndex(w.compression_controls.mode.findData('target_size'))
w.compression_controls.amount.setValue(1950)
w.start_scan([str(source)])
deadline=time.monotonic()+20
while w.busy or w.resize_controls.worker:
    if time.monotonic()>deadline:raise TimeoutError('Capture intake timed out')
    QTest.qWait(20)
QTest.qWait(150)
while w.resize_controls.worker or w.resize_controls.timer.isActive():
    if time.monotonic()>deadline:raise TimeoutError('Dimension preview timed out')
    QTest.qWait(20)
for width,height in ((1180,740),(880,610)):
    w.resize(width,height)
    for theme in ('dark','light'):
        w.set_theme(theme,persist=False)
        for stage,control in (('resize',w.resize_controls.dimensions),('compression',w.compression_controls.target_panel)):
            w.settings_scroll.ensureWidgetVisible(control,0,10)
            QTest.qWait(120)
            assert w.grab().save(str(out/f'{stage}-{theme}-{width}.png'))
        w.compression_controls.mode.setCurrentIndex(w.compression_controls.mode.findData('quality'))
        w.settings_scroll.ensureWidgetVisible(w.compression_controls.panel,0,10)
        QTest.qWait(120)
        assert w.grab().save(str(out/f'quality-{theme}-{width}.png'))
        w.compression_controls.mode.setCurrentIndex(w.compression_controls.mode.findData('target_size'))
w.close()
QTest.qWait(100)
print(out)
