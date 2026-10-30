"""Hand cursors for actionable controls, with normal text/resize cursors intact."""
from PySide6.QtCore import QObject,QEvent,Qt
from PySide6.QtWidgets import (QApplication,QWidget,QAbstractButton,QComboBox,QSlider,
    QScrollBar,QTabBar,QMenu,QAbstractItemView,QHeaderView,QAbstractSpinBox,QStyle,QStyleOptionSpinBox)


class InteractionCursors(QObject):
    def __init__(self,host):
        super().__init__(host)
        self.host=host
        QApplication.instance().installEventFilter(self)
        for widget in host.findChildren(QWidget):self.prepare(widget)

    def prepare(self,widget):
        if isinstance(widget,(QAbstractButton,QComboBox,QSlider,QScrollBar)):
            widget.setCursor(Qt.CursorShape.PointingHandCursor if widget.isEnabled() else Qt.CursorShape.ArrowCursor)
        if isinstance(widget,(QTabBar,QMenu,QAbstractSpinBox)):
            widget.setMouseTracking(True)
        elif isinstance(widget,QAbstractItemView):
            widget.viewport().setMouseTracking(True)
            if not widget.isEnabled():widget.viewport().setCursor(Qt.CursorShape.ArrowCursor)

    def eventFilter(self,widget,event):
        kind=event.type()
        if kind not in (QEvent.Type.Show,QEvent.Type.Enter,QEvent.Type.EnabledChange,QEvent.Type.MouseMove):return False
        if not isinstance(widget,QWidget) or not self.host.isAncestorOf(widget):return False
        if kind in (QEvent.Type.Show,QEvent.Type.EnabledChange):
            self.prepare(widget)
            return False
        if isinstance(widget,(QAbstractButton,QComboBox,QSlider,QScrollBar)):
            self.prepare(widget)
            return False
        pos=event.position().toPoint()
        clickable=False
        if isinstance(widget,QTabBar):
            index=widget.tabAt(pos)
            clickable=index>=0 and widget.isTabEnabled(index)
        elif isinstance(widget,QMenu):
            action=widget.actionAt(pos)
            clickable=bool(action and action.isEnabled() and not action.isSeparator())
        elif isinstance(widget,QAbstractSpinBox):
            option=QStyleOptionSpinBox()
            widget.initStyleOption(option)
            part=widget.style().hitTestComplexControl(QStyle.ComplexControl.CC_SpinBox,option,pos,widget)
            clickable=part in (QStyle.SubControl.SC_SpinBoxUp,QStyle.SubControl.SC_SpinBoxDown)
        elif isinstance(widget.parentWidget(),QHeaderView):
            owner=widget.parentWidget()
            index=owner.logicalIndexAt(pos)
            if index<0 or not owner.sectionsClickable():return False
            coordinate=pos.x() if owner.orientation()==Qt.Orientation.Horizontal else pos.y()
            offset=coordinate-owner.sectionViewportPosition(index)
            # Let Qt manage the resize handle at either edge of a section.
            if min(offset,owner.sectionSize(index)-offset)<5:return False
            clickable=True
        elif isinstance(widget.parentWidget(),QAbstractItemView):
            owner=widget.parentWidget()
            index=owner.indexAt(pos)
            clickable=index.isValid() and bool(index.flags() & Qt.ItemFlag.ItemIsEnabled)
        else:return False
        shape=Qt.CursorShape.PointingHandCursor if clickable and widget.isEnabled() else Qt.CursorShape.ArrowCursor
        if widget.cursor().shape()!=shape:widget.setCursor(shape)
        return False
