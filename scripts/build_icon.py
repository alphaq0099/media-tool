"""Render the editable SVG into the multi-resolution Windows icon."""
from io import BytesIO
from pathlib import Path
import os

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PIL import Image
from PySide6.QtCore import QBuffer,QByteArray,QIODevice,Qt
from PySide6.QtGui import QGuiApplication,QImage,QPainter
from PySide6.QtSvg import QSvgRenderer

root=Path(__file__).resolve().parents[1]
folder=root/'src/media_tool/icons'
app=QGuiApplication([])
renderer=QSvgRenderer(str(folder/'media-tool.svg'))
assert renderer.isValid()
image=QImage(256,256,QImage.Format.Format_ARGB32_Premultiplied)
image.fill(Qt.GlobalColor.transparent)
painter=QPainter(image)
renderer.render(painter)
painter.end()
data=QByteArray()
buffer=QBuffer(data)
buffer.open(QIODevice.OpenModeFlag.WriteOnly)
assert image.save(buffer,'PNG')
buffer.close()
with Image.open(BytesIO(bytes(data))) as raster:
    raster.save(folder/'media-tool.ico',sizes=[(n,n) for n in (16,24,32,48,64,128,256)])
print(folder/'media-tool.ico')
