"""Capture the real extension filters with isolated synthetic inputs."""
import os
from pathlib import Path
import sys

os.environ['QT_QPA_PLATFORM']='windows'
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'src'))
from PySide6.QtCore import QSettings,Qt
from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from media_tool.domain import FileRecord,category
from media_tool.window import Workspace

out=root/'artifacts'/'input-types-captures'
out.mkdir(exist_ok=True)
app=QApplication([])
app.setStyle('Fusion')
app.setFont(QFont('Segoe UI',10))
settings=QSettings(str(out/'capture.ini'),QSettings.Format.IniFormat)
settings.clear()
w=Workspace(settings)
w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen,True)
w.show()
names=('Portrait.jpg','Landscape.png','Banner.webp','Scanned page.tif','Animation.gif','Holiday.mkv','Interview.mp4','Music.mp3','Recording.wav','Notes.txt')
w.model.append([FileRecord('sample://'+name,name,1024*(i+1),category(Path(name)),True) for i,name in enumerate(names)])
for width,height in ((1180,740),(880,610)):
    w.resize(width,height)
    for tool,choice in (('Images','All images'),('Images','PNG'),('Video','All videos'),('Audio','All audio'),('Audio','Video sources')):
        w._tool_changed(tool)
        w.category_filter.setCurrentText(choice)
        QTest.qWait(80)
        name=f'{tool.lower()}-{choice.lower().replace(" ","-")}-{width}.png'
        w.grab().save(str(out/name))
w.close()
QTest.qWait(50)
print(out)
