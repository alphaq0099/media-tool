"""Small keyboard-accessible info tooltips; keep prose out of working forms."""
from PySide6.QtCore import Qt,QEvent,QObject
from PySide6.QtWidgets import QToolButton,QToolTip,QWidget,QApplication


class InfoButton(QToolButton):
    def __init__(self,description,parent=None,name='Help'):
        super().__init__(parent)
        self.description=description
        self.setObjectName('info')
        self.setText('i')
        self.setFixedSize(20,20)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(name)
        self.update_tip()
    def update_tip(self):
        value=self.description() if callable(self.description) else self.description
        self.setToolTip(value)
        self.setAccessibleDescription(value)
    def explain(self):
        self.update_tip()
        QToolTip.showText(self.mapToGlobal(self.rect().bottomLeft()),self.toolTip(),self)
    def event(self,event):
        if event.type() in (QEvent.Type.Enter,QEvent.Type.FocusIn,QEvent.Type.ToolTip):self.update_tip()
        return super().event(event)


class InfoHoverOnly(QObject):
    """Retain help text for (i)/accessibility, suppress incidental hover popups."""
    def __init__(self,host):
        super().__init__(host)
        self.host=host
        QApplication.instance().installEventFilter(self)

    def eventFilter(self,watched,event):
        if event.type()==QEvent.Type.ToolTip and isinstance(watched,QWidget):
            if watched is self.host or self.host.isAncestorOf(watched):
                return not isinstance(watched,InfoButton)
        return False

    def stop(self):
        QApplication.instance().removeEventFilter(self)
