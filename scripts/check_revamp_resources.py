"""Native navigation/sample-table resource check using the actual GUI process."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

os.environ['QT_QPA_PLATFORM']='windows'
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'src'))
import psutil
from PySide6.QtCore import QSettings,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QWidget
from media_tool.window import Workspace

parser=argparse.ArgumentParser()
parser.add_argument('--report',type=Path,default=root/'artifacts'/'revamp-navigation-resources.json')
args=parser.parse_args()
out=args.report.resolve().parent/(args.report.stem+'-fixtures')
out.mkdir(exist_ok=True)
app=QApplication([])
settings=QSettings(str(out/'settings.ini'),QSettings.Format.IniFormat)
settings.clear()
w=Workspace(settings)
w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen,True)
w.show()
w.start_scan(sample_count=10000)
deadline=time.monotonic()+15
while w.busy:
    if time.monotonic()>deadline:raise RuntimeError('Sample intake timed out')
    QTest.qWait(10)
tools=('Images','Video','Audio','Rename','Archives','Documents','Folders','Queue')
def navigate(tool):
    if tool=='Folders':w.show_folders()
    elif tool=='Queue':w.open_jobs()
    else:w._tool_changed(tool)
    QTest.qWait(15)
for tool in tools:navigate(tool)
process=psutil.Process(os.getpid())
def snapshot():
    memory=process.memory_info()
    return dict(working_set_MiB=round(memory.rss/2**20,2),
        private_MiB=round(memory.private/2**20,2),handles=process.num_handles(),
        widgets=len(w.findChildren(QWidget)))
before=snapshot()
latencies=[]
for _ in range(10):
    for tool in tools:
        started=time.perf_counter()
        navigate(tool)
        latencies.append(time.perf_counter()-started)
w._tool_changed('Images')
QTest.qWait(250)
after=snapshot()
assert w.model.rowCount()==10000
assert after['widgets']==before['widgets'],(before,after)
assert not w.visual.activity.timer.isActive()
assert w.waveform.worker is None
children=[p.name() for p in process.children(recursive=True)]
assert not children,children
process.cpu_percent(None)
QTest.qWait(1000)
idle_cpu=process.cpu_percent(None)
result=dict(passed=True,platform=app.platformName(),native_platform_offscreen=True,sample_rows=10000,navigation_changes=80,
    before=before,after=after,private_growth_MiB=round(after['private_MiB']-before['private_MiB'],2),
    max_navigation_seconds_including_15ms_paint_wait=round(max(latencies),4),
    idle_cpu_percent_one_core=idle_cpu,child_processes=children,
    note='Warm source navigation after all pages were loaded; not a packaged or cold-start test.')
args.report.write_text(json.dumps(result,indent=2))
print(json.dumps(result,indent=2))
w.close()
deadline=time.monotonic()+10
while w.isVisible():
    if time.monotonic()>deadline:raise RuntimeError('Close timed out')
    QTest.qWait(20)
