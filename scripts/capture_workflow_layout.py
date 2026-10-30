"""Native Qt captures of the annotated workflow/history update, using disposable data."""
import os
from pathlib import Path
import sys
import tempfile

os.environ['QT_QPA_PLATFORM']='windows'
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'src'))
from dataclasses import asdict
from PIL import Image
from PySide6.QtCore import QSettings,Qt
from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from media_tool.domain import FileRecord
from media_tool.jobs import execute_job
from media_tool.planning import PlanRequest
from media_tool.window import Workspace
from media_tool.workflow_ui import record_data
from media_tool.workflows import workflow_snapshot

out=root/'artifacts'/'workflow-layout-captures'
out.mkdir(exist_ok=True)
app=QApplication([])
app.setStyle('Fusion')
app.setFont(QFont('Segoe UI',10))

def step(operation,label,**options):return dict(label=label,request=asdict(PlanRequest(operation,**options)))

with tempfile.TemporaryDirectory(prefix='workflow-capture-',dir=root/'artifacts') as scratch:
    work=Path(scratch)
    source=work/'4.png'
    Image.new('RGB',(24,16),'teal').save(source)
    record=FileRecord.from_path(source)
    w=Workspace(QSettings(str(work/'settings.ini'),QSettings.Format.IniFormat))
    w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen,True)
    w.show()
    w.open_jobs()
    q=w.jobs_dialog
    for title,name in (('Earlier video conversion','Holiday.mp4'),('Earlier image export','Portrait.webp')):
        job=q.store.enqueue(title,'sample',[dict(name=name,seconds=0)])
        assert execute_job(q.store,job)=='succeeded'
    steps=[step('images','Images',extension='jpg',image_quality=100),
        step('rename','Rename',prefix='image',padding=1),step('archives','Archive',archive_format='zip',archive_name='Photos')]
    job=q.store.enqueue('Photo workflow','workflow',workflow_snapshot([record],steps))
    assert execute_job(q.store,job)=='succeeded'
    e=q.workflow_editor
    e.inputs=[record_data(record)]
    e.steps=[*steps[:2],step('archives','Archive',archive_format='7z',archive_name='Photos',archive_encrypt=True)]
    e.refresh()
    e.message('Step saved.')
    for width,height in ((1180,740),(880,610),(1600,900)):
        w.resize(width,height)
        q.tabs.setCurrentWidget(e)
        e.step_list.setCurrentRow(0)
        QTest.qWait(100)
        w.grab().save(str(out/f'workflow-{width}.png'))
        q.tabs.setCurrentWidget(q.views.history)
        q.refresh(job)
        q.items_table.selectRow(2)
        q.history_layout.tabs.setCurrentIndex(0)
        QTest.qWait(100)
        assert q.jobs_splitter.count()==2
        w.grab().save(str(out/f'history-steps-{width}.png'))
        q.history_layout.tabs.setCurrentIndex(1)
        QTest.qWait(80)
        w.grab().save(str(out/f'history-files-{width}.png'))
    w.close()
    while w.isVisible():QTest.qWait(20)
    w.deleteLater()
    QTest.qWait(30)
print(out)
