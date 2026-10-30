"""Native result at the screenshot's JPEG / 150 DPI / quality 100 settings."""
import json
import os
from pathlib import Path
import shutil
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
from PIL import Image
from media_tool.domain import FileRecord
from media_tool.window import Workspace
from check_documents_prototypes import text_document,digest

art=root/'artifacts'
app=QApplication([])
app.setStyle('Fusion')
app.setFont(QFont('Segoe UI',10))
with tempfile.TemporaryDirectory(prefix='document-native-',dir=art) as scratch:
    base=Path(scratch)
    source=base/'Word-style final.pdf'
    text_document(source)
    before=digest(source)
    w=Workspace(QSettings(str(base/'settings.ini'),QSettings.Format.IniFormat))
    w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen,True)
    w.show()
    w._tool_changed('Documents')
    c=w.document_controls
    c.task.setCurrentIndex(c.task.findData('render'))
    c.pages.setText('1')
    c.dpi.setValue(150)
    c.quality.setValue(100)
    w.format.setCurrentText('JPEG')
    w.model.append([FileRecord.from_path(source)])
    w.start_rename_button.click()
    deadline=time.monotonic()+30
    while w.busy or w.tabs.currentIndex()!=2:
        if time.monotonic()>deadline:raise TimeoutError('Native document render timed out.')
        QTest.qWait(20)
    item=w.result_model.items[0]
    assert item['state']=='succeeded',item
    assert digest(source)==before
    with Image.open(item['output']) as image:
        assert image.size==(1241,1754)
        result=dict(passed=True,state=item['state'],size=list(image.size),format=image.format,dpi=150,quality=100,original_unchanged=True)
    QTest.qWait(100)
    w.grab().save(str(art/'documents-render-native.png'))
    shutil.copyfile(item['output'],art/'documents-render-sample.jpg')
    w.close()
    while w.isVisible():QTest.qWait(20)
    w.deleteLater();QTest.qWait(30)
result['fixture_removed']=not base.exists()
(art/'documents-render-native.json').write_text(json.dumps(result,indent=2))
print(json.dumps(result,indent=2))
