"""Settings controls keep their values while the user scrolls the panel."""
from PySide6.QtCore import QObject, QEvent, QCoreApplication, QPointF
from PySide6.QtGui import QWheelEvent


class SettingsWheelGuard(QObject):
    def __init__(self, scroll):
        super().__init__(scroll)
        self.scroll = scroll

    def eventFilter(self, control, event):
        if event.type() == QEvent.Type.Wheel:
            viewport = self.scroll.viewport()
            forwarded = QWheelEvent(
                QPointF(viewport.mapFromGlobal(event.globalPosition().toPoint())),
                event.globalPosition(), event.pixelDelta(), event.angleDelta(),
                event.buttons(), event.modifiers(), event.phase(), event.inverted(),
                event.source(), event.pointingDevice())
            QCoreApplication.sendEvent(viewport, forwarded)
            event.accept()
            return True
        return super().eventFilter(control, event)
