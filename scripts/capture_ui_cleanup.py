"""Capture Folders and Queue using generated inputs and isolated settings."""
from pathlib import Path
import os
import sys
import time

root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'src'))
os.environ.setdefault('QT_QPA_PLATFORM','windows' if os.name=='nt' else 'offscreen')
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QSettings,Qt
from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from media_tool.window import Workspace

out=root/'artifacts/ui-cleanup'
out.mkdir(parents=True,exist_ok=True)
app=QApplication([])
app.setStyle('Fusion')
app.setFont(QFont('Segoe UI' if os.name=='nt' else 'Sans Serif',10))
settings=QSettings(str(out/'captures.ini'),QSettings.IniFormat)
settings.clear()
w=Workspace(settings)
w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen,True)
w.show()
source=out/'sample-folders'
(source/'Photos'/'Originals').mkdir(parents=True,exist_ok=True)
(source/'Documents').mkdir(exist_ok=True)
(source/'Photos'/'Originals'/'sample.jpg').write_bytes(b'fixture'*200)
(source/'Documents'/'sample.pdf').write_bytes(b'fixture'*500)
page=w.show_folders()
page.root.setText(str(source));page.scan()
deadline=time.monotonic()+20
while w.busy:
    if time.monotonic()>deadline:raise TimeoutError('Folder capture scan timed out')
    QTest.qWait(20)
page.tree.expandAll()
for theme in ('dark','light'):
    w.set_theme(theme,persist=False);QTest.qWait(150)
    assert w.grab().save(str(out/f'folders-{theme}.png'))
w.set_theme('dark',persist=False)
w.open_jobs()
queue=w.jobs_dialog
job=queue.store.enqueue('Image conversion','sample',[dict(name='sample.png',seconds=0)])
queue.refresh(job)
for theme in ('dark','light'):
    queue.tabs.setCurrentWidget(queue.views.history)
    w.set_theme(theme,persist=False);QTest.qWait(150)
    assert w.grab().save(str(out/f'queue-{theme}.png'))
    queue.tabs.setCurrentWidget(queue.workflow_editor);QTest.qWait(100)
    assert w.grab().save(str(out/f'workflow-{theme}.png'))
w.close();QTest.qWait(100)
print(out)
