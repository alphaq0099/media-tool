import json, sys, time
from pathlib import Path
sys.path.insert(0,str(Path("src").resolve()))
from PySide6.QtCore import Qt, QTimer, QSettings
from PySide6.QtWidgets import QApplication
from media_tool.window import Workspace
app=QApplication([])
app.setStyle("Fusion")
window=Workspace(QSettings(str(Path("artifacts/check.ini").resolve()),QSettings.Format.IniFormat))
window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen,True)
window.show()
ticks=[]
timer=QTimer()
timer.setInterval(10)
timer.timeout.connect(lambda:ticks.append(time.perf_counter()))
report={}
started=0
def begin():
    global started
    started=time.perf_counter()
    timer.start()
    window.load_samples()
def finished():
    report["rows"]=window.model.rowCount()
    report["load_and_sort_ms"]=round((time.perf_counter()-started)*1000,2)
    report["note"]="Synthetic worker is deliberately paced; this is a UI responsiveness fixture, not disk-scan throughput."
    QTimer.singleShot(60,finish)
def finish():
    timer.stop()
    report["max_heartbeat_gap_ms"]=round(max((b-a)*1000 for a,b in zip(ticks,ticks[1:])),2)
    begin_toggle=time.perf_counter()
    window.toggle_theme()
    report["theme_change_dispatch_ms"]=round((time.perf_counter()-begin_toggle)*1000,2)
    report["native_platform"]=app.platformName()
    Path("artifacts/responsiveness.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps(report,indent=2),flush=True)
    window.close()
window.scan_complete.connect(finished)
QTimer.singleShot(100,begin)
QTimer.singleShot(10000,window.close)
app.exec()
