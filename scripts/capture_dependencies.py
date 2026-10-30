"""Capture real local dependency checks; never download or install a package."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time

os.environ['QT_QPA_PLATFORM']='windows'
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'src'))
from PySide6.QtCore import QSettings,Qt
from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from media_tool.window import Workspace

out=root/'artifacts'/'dependency-captures'
out.mkdir(exist_ok=True)
app=QApplication([])
app.setStyle('Fusion')
app.setFont(QFont('Segoe UI',10))
with tempfile.TemporaryDirectory(prefix='dependencies-native-',dir=root/'artifacts') as scratch:
    w=Workspace(QSettings(str(Path(scratch)/'settings.ini'),QSettings.Format.IniFormat))
    w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen,True)
    w.show()
    w.dependencies_action.trigger()
    page=w.dependencies_page
    deadline=time.monotonic()+40
    while page.busy or page.activation.isActive():
        if time.monotonic()>deadline:raise TimeoutError('Dependency checks timed out.')
        QTest.qWait(20)
    page.select_all_missing()
    for theme in ('dark','light'):
        w.set_theme(theme,persist=False)
        for width,height in ((1180,740),(880,610)):
            w.resize(width,height)
            QTest.qWait(100)
            w.grab().save(str(out/f'dependencies-{theme}-{width}.png'))
    result=dict(passed=True,states=page.states,winget_usable=bool(page.winget),
        probe_results=page.results,real_installations_performed=0,downloads_performed=0,
        platform=app.platformName(),native_platform_offscreen=True,
        note='Actual local detection. Controlled installer fixtures are tested separately; no real package installation is claimed.')
    w.close()
    while w.isVisible():QTest.qWait(20)
    w.deleteLater();QTest.qWait(30)
result['fixture_removed']=not Path(scratch).exists()
(root/'artifacts'/'dependencies-native.json').write_text(json.dumps(result,indent=2))
print(json.dumps({key:value for key,value in result.items() if key!='probe_results'},indent=2))
