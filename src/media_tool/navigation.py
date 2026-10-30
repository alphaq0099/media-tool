"""Small, scalable sidebar icons painted without extra image dependencies."""
from functools import lru_cache
from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap

ICON_SIZE = QSize(20, 20)

@lru_cache(maxsize=64)
def navigation_icon(title, color):
    pixels = QPixmap(48, 48)
    pixels.setDevicePixelRatio(2)
    pixels.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixels)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(color), 1.8, Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    if title == "Images":
        painter.drawRoundedRect(QRectF(3, 4, 18, 16), 2, 2)
        painter.drawEllipse(QRectF(6, 7, 3, 3))
        painter.drawLine(4, 17, 10, 12)
        painter.drawLine(10, 12, 14, 16)
        painter.drawLine(14, 16, 18, 12)
        painter.drawLine(18, 12, 21, 15)
    elif title == "Video":
        painter.drawRoundedRect(QRectF(3, 4, 18, 16), 2, 2)
        for y in (8, 16):
            painter.drawLine(3, y, 21, y)
        for x in (8, 16):
            painter.drawLine(x, 4, x, 8)
            painter.drawLine(x, 16, x, 20)
        painter.drawLine(10, 10, 15, 12)
        painter.drawLine(15, 12, 10, 14)
        painter.drawLine(10, 14, 10, 10)
    elif title == "Audio":
        painter.drawLine(10, 5, 10, 17)
        painter.drawLine(10, 5, 20, 3)
        painter.drawLine(20, 3, 20, 15)
        painter.drawEllipse(QRectF(4, 15, 6, 5))
        painter.drawEllipse(QRectF(14, 13, 6, 5))
    elif title == "Documents":
        painter.drawRoundedRect(QRectF(5,3,14,18),1,1)
        for y in (8,12,16):
            painter.drawLine(8,y,16,y)
    elif title == "Archives":
        painter.drawRoundedRect(QRectF(4, 3, 16, 18), 2, 2)
        for y in (5, 8, 11, 14):
            painter.drawLine(11, y, 14, y)
        painter.drawRoundedRect(QRectF(11, 16, 3, 3), 1, 1)
    elif title == "Folders":
        painter.drawLine(3, 7, 3, 20)
        painter.drawLine(3, 20, 21, 20)
        painter.drawLine(3, 7, 10, 7)
        painter.drawLine(10, 7, 12, 10)
        painter.drawLine(12, 10, 21, 10)
        painter.drawLine(21, 10, 21, 20)
        painter.drawLine(7, 16, 7, 18)
        painter.drawLine(12, 14, 12, 18)
        painter.drawLine(17, 12, 17, 18)
    elif title == "Rename":
        painter.drawLine(3, 6, 17, 6)
        painter.drawLine(10, 6, 10, 19)
        painter.drawLine(6, 19, 14, 19)
        painter.drawLine(18, 11, 21, 14)
        painter.drawLine(21, 14, 18, 17)
        painter.drawLine(15, 14, 21, 14)
    else:
        for y in (6, 12, 18):
            painter.drawEllipse(QRectF(3, y - 1, 2, 2))
            painter.drawLine(9, y, 21, y)
    painter.end()
    return QIcon(pixels)

