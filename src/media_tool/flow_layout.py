"""Wrapping workspace toolbars with the same insertion API as a horizontal row."""
from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QSizePolicy, QSpacerItem, QWidgetItem


class FlowLayout(QLayout):
    def __init__(self, spacing=8):
        super().__init__()
        self._items = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(spacing)

    def addItem(self, item):
        self._items.append((item, 0))
        self.invalidate()

    def addWidget(self, widget, stretch=0, alignment=Qt.AlignmentFlag(0)):
        self.insertWidget(self.count(), widget, stretch, alignment)

    def insertWidget(self, index, widget, stretch=0, alignment=Qt.AlignmentFlag(0)):
        self.addChildWidget(widget)
        item = QWidgetItem(widget)
        item.setAlignment(alignment)
        self._items.insert(max(0, min(index, self.count())), (item, stretch))
        self.invalidate()

    def addStretch(self, stretch=1):
        self._items.append((QSpacerItem(0, 0, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum), stretch))
        self.invalidate()

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index][0] if 0 <= index < self.count() else None

    def takeAt(self, index):
        if 0 <= index < self.count():
            item = self._items.pop(index)[0]
            self.invalidate()
            return item

    def expandingDirections(self):
        return Qt.Orientation.Horizontal

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._arrange(QRect(0, 0, width, 0), False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item, _ in self._items:
            if item.widget() is None or not item.widget().isHidden():
                size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, True)

    def _arrange(self, rect, apply):
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        width = max(1, area.width())
        rows, current, used = [], [], 0
        for item, stretch in self._items:
            widget = item.widget()
            if widget is not None and widget.isHidden():
                continue
            hint = item.sizeHint()
            preferred = widget.property("flowWidth") if widget is not None else None
            item_width = min(width, max(item.minimumSize().width(), int(preferred) if preferred else hint.width()))
            gap = self.spacing() if current and widget is not None else 0
            if current and widget is not None and used + gap + item_width > width:
                rows.append(current)
                current, used, gap = [], 0, 0
            current.append((item, stretch, item_width, max(0, hint.height())))
            used += gap + item_width
        if current:
            rows.append(current)
        y = area.y()
        for row in rows:
            widgets = sum(item.widget() is not None for item, _, _, _ in row)
            gaps = max(0, widgets - 1) * self.spacing()
            extra = max(0, width - sum(w for _, _, w, _ in row) - gaps)
            weight = sum(stretch for _, stretch, _, _ in row)
            height = max((h for _, _, _, h in row), default=0)
            x, placed = area.x(), False
            for item, stretch, item_width, _ in row:
                if item.widget() is not None and placed:
                    x += self.spacing()
                allocated = item_width + (extra * stretch // weight if weight else 0)
                if apply:
                    item.setGeometry(QRect(x, y, allocated, height))
                x += allocated
                if item.widget() is not None:
                    placed = True
            y += height + self.spacing()
        return y - area.y() - (self.spacing() if rows else 0) + margins.top() + margins.bottom()
