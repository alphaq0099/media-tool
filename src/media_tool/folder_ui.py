"""Lazy folder inspector and previewed flatten/undo workspace."""
from collections import Counter
import json
from pathlib import Path
import subprocess
import sys
import time

from PySide6.QtCore import QAbstractItemModel, QModelIndex, Qt, QThread, QTimer, Signal, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
    QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QProgressBar,
    QPushButton, QScrollArea, QSpinBox, QSplitter, QTabWidget, QTreeView,
    QVBoxLayout, QWidget, QSizePolicy, QAbstractSpinBox,
)

from .domain import format_size
from .flow_layout import FlowLayout
from .folders import folder_view, scan_folder
from .folder_moves import cleanup_rows, plan_flatten, plan_folder_undo, projected_snapshot
from .jobs import Cancelled, Store
from .jobs_ui import Rows, table


def label(value, name=None):
    item = QLabel(value)
    item.setTextFormat(Qt.TextFormat.PlainText)
    item.setWordWrap(True)
    if name:
        item.setObjectName(name)
    return item


def button(value, callback, name=None):
    item = QPushButton(value)
    if name:
        item.setObjectName(name)
    item.clicked.connect(callback)
    return item


class SizeBox(QDoubleSpinBox):
    def __init__(self):
        super().__init__()
        self.selection_timer = QTimer(self)
        self.selection_timer.setSingleShot(True)
        self.selection_timer.timeout.connect(self.selectAll)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        if event.reason() in (Qt.FocusReason.MouseFocusReason, Qt.FocusReason.TabFocusReason,
                              Qt.FocusReason.BacktabFocusReason):
            self.selection_timer.start(0)


class CheckCaption(QLabel):
    """Clickable wrapping text keeps checkbox rows usable with wider fonts."""
    def __init__(self, value, control):
        super().__init__(value)
        self.control = control
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(True)
        self.setBuddy(control)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.control.click()
            event.accept()
        else:
            super().mouseReleaseEvent(event)


def wrapped_check(control):
    value = control.text()
    control.setAccessibleName(value)
    control.setText("")
    row = QWidget()
    row.setToolTip(control.toolTip())
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(7)
    layout.addWidget(control, 0, Qt.AlignmentFlag.AlignTop)
    layout.addWidget(CheckCaption(value, control), 1)
    return row


class FolderTreeModel(QAbstractItemModel):
    """No per-file widgets; only expanded rows enter the view, in small chunks."""
    headers = ("Name", "Total size", "Files", "Depth", "Status")
    chunk = 256

    def __init__(self, parent=None):
        super().__init__(parent)
        self.view = None
        self.loaded = {}

    def set_view(self, view):
        self.beginResetModel()
        self.view = view
        self.loaded = {0: min(self.chunk, len(view.children[0]))} if view else {}
        self.endResetModel()

    def columnCount(self, parent=QModelIndex()):
        return len(self.headers)

    def rowCount(self, parent=QModelIndex()):
        if self.view is None or (parent.isValid() and parent.column()):
            return 0
        return self.loaded.get(parent.internalId() - 1, 0) if parent.isValid() else 1

    def index(self, row, column, parent=QModelIndex()):
        if not self.hasIndex(row, column, parent) or self.view is None:
            return QModelIndex()
        node = self.view.children[parent.internalId() - 1][row] if parent.isValid() else 0
        return self.createIndex(row, column, node + 1)

    def parent(self, index):
        if not index.isValid() or self.view is None:
            return QModelIndex()
        node = self.view.snapshot.nodes[index.internalId() - 1]
        if node.parent < 0:
            return QModelIndex()
        return self.createIndex(self.view.positions[node.parent], 0, node.parent + 1)

    def hasChildren(self, parent=QModelIndex()):
        if self.view is None or (parent.isValid() and parent.column()):
            return False
        return bool(self.view.children[parent.internalId() - 1]) if parent.isValid() else True

    def canFetchMore(self, parent):
        return bool(parent.isValid() and parent.column() == 0 and self.view is not None
                    and self.loaded.get(parent.internalId() - 1, 0) < len(self.view.children[parent.internalId() - 1]))

    def fetchMore(self, parent):
        if not self.canFetchMore(parent):
            return
        node = parent.internalId() - 1
        start = self.loaded.get(node, 0)
        end = min(start + self.chunk, len(self.view.children[node]))
        self.beginInsertRows(parent, start, end - 1)
        self.loaded[node] = end
        self.endInsertRows()

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            if section == 1 and self.view and (self.view.min_bytes or self.view.max_bytes is not None):
                return "Size (filtered)"
            return self.headers[section]
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.ToolTipRole:
            return ("Size and file count include only matching files when a size filter is active. "
                    "Hover a row to see both filtered and full totals.")

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or self.view is None:
            return None
        number = index.internalId() - 1
        node, column = self.view.snapshot.nodes[number], index.column()
        if role == Qt.ItemDataRole.ToolTipRole:
            return (f"{node.path}\n{node.issue or node.kind}\n"
                    f"Total: {node.total:,} bytes; matching: {self.view.matching_bytes[number]:,} bytes.\n"
                    f"Files: {node.files:,} total; {self.view.matching_files[number]:,} matching.\n"
                    "Totals include all scanned descendants, independent of display depth. "
                    "Logical bytes per file entry; hard links count per name; links are excluded. "
                    "A snapshot can become stale; Refresh before planning moves.")
        if role == Qt.ItemDataRole.DisplayRole:
            partial = " (partial)" if node.partial else ""
            filtered = self.view.min_bytes or self.view.max_bytes is not None
            return (node.name,
                    format_size(self.view.matching_bytes[number] if filtered else node.total) + partial,
                    f"{self.view.matching_files[number] if filtered else node.files:,}",
                    str(node.depth),
                    "Partial" if node.partial else "Excluded" if node.kind in ("Link", "Excluded") else node.kind)[column]


def tree(model):
    view = QTreeView()
    view.setModel(model)
    view.setUniformRowHeights(True)
    view.setAlternatingRowColors(True)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.setMinimumWidth(0)
    view.header().setMinimumSectionSize(70)
    view.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    view.header().setStretchLastSection(False)
    view.setColumnWidth(0, 230)
    for column, width in ((1, 125), (2, 80), (3, 65), (4, 90)):
        view.setColumnWidth(column, width)
    return view


class FolderWorker(QThread):
    progress = Signal(int)

    def __init__(self, kind, work, parent):
        super().__init__(parent)
        self.kind, self.work = kind, work
        self.result = None
        self.error = None
        self.last_progress = 0

    def report(self, count):
        stamp = time.monotonic()
        if stamp - self.last_progress > 0.05:
            self.last_progress = stamp
            self.progress.emit(count)

    def run(self):
        try:
            self.result = self.work(self.isInterruptionRequested, self.report)
        except Exception as error:
            self.error = error


class FolderWorkspace(QWidget):
    def __init__(self, host):
        super().__init__()
        self.host = host
        self.worker = None
        self.snapshot = None
        self.plan = None
        self.result_job = None
        self.result_undoable = False
        self.sort_column = 0
        self.sort_descending = False
        self.setObjectName("workspace")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground,True)
        main = QVBoxLayout(self)
        main.setContentsMargins(18, 16, 18, 14)
        main.setSpacing(12)
        heading=QHBoxLayout()
        heading.addWidget(label("Inspect & flatten folders", "heading"),1)
        from .help_ui import InfoButton
        heading.addWidget(InfoButton(lambda:'Inspect folder sizes or move nested content up one level.'+
            ('\n\n'+self.summary.toolTip() if hasattr(self,'summary') else ''),name='Folder tool information'))
        main.addLayout(heading)
        columns = QHBoxLayout()
        columns.setSpacing(14)
        files = QFrame()
        files.setObjectName("panel")
        contents = QVBoxLayout(files)
        contents.setContentsMargins(14, 10, 14, 12)
        contents.setSpacing(8)
        path_row = QHBoxLayout()
        self.root = QLineEdit(str(host.settings.value("folder_root", "")))
        self.root.setPlaceholderText("Choose or drop a root folder")
        self.root.setAccessibleName("Root folder")
        self.root.returnPressed.connect(self.scan)
        self.root.textEdited.connect(self.discard_snapshot)
        self.open_button = button("Open folder", self.choose_folder)
        path_row.addWidget(self.open_button)
        path_row.addWidget(self.root, 1)
        contents.addLayout(path_row)
        tools = FlowLayout()
        self.refresh_button = button("Refresh", self.scan)
        self.reveal_button = button("Reveal", self.reveal)
        self.clear_button = button("Clear", self.clear)
        tools.addWidget(self.refresh_button)
        tools.addWidget(self.reveal_button)
        tools.addStretch()
        tools.addWidget(self.clear_button)
        contents.addLayout(tools)
        self.tabs = QTabWidget()
        self.tree_model = FolderTreeModel(self)
        self.tree = tree(self.tree_model)
        self.tree.header().setSectionsClickable(True)
        self.tree.header().setSortIndicatorShown(True)
        self.tree.header().setSortIndicator(0, Qt.SortOrder.AscendingOrder)
        self.tree.header().setToolTip("Click a column heading to sort. Click it again to reverse the order.")
        self.tree.header().sectionClicked.connect(self.sort_clicked)
        self.tree.setAccessibleName("Inspected folder tree")
        self.tabs.addTab(self.tree, "Folders")
        self.preview_tabs = QTabWidget()
        self.moves_model = Rows(("Before", "After", "State", "Details"), self)
        self.moves_table = table(self.moves_model)
        self.moves_table.setAccessibleName("Folder move preview")
        self.preview_tabs.addTab(self.moves_table, "Moves")
        self.before_model, self.after_model = FolderTreeModel(self), FolderTreeModel(self)
        self.before_tree, self.after_tree = tree(self.before_model), tree(self.after_model)
        split = QSplitter()
        for title, view in (("Before", self.before_tree), ("After", self.after_tree)):
            pane = QWidget()
            layout = QVBoxLayout(pane)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(label(title, "section"))
            layout.addWidget(view)
            view.setAccessibleName(title + " folder tree")
            split.addWidget(pane)
        split.setChildrenCollapsible(False)
        self.preview_tabs.addTab(split, "Before / After")
        self.tabs.addTab(self.preview_tabs, "Preview")
        self.results_model = Rows(("Source", "Output", "State", "Details"), self)
        self.results_table = table(self.results_model)
        self.results_table.setAccessibleName("Folder move results")
        self.tabs.addTab(self.results_table, "Results")
        contents.addWidget(self.tabs, 1)
        self.summary = label("No folder scanned", "muted")
        self.summary.setToolTip("Min/max filters select individual files. Folder matching totals sum those files. Display depth and folders-only view do not change totals.")
        contents.addWidget(self.summary)
        self.issues_button = button("Scan issues", self.show_issues)
        self.issues_button.hide()
        contents.addWidget(self.issues_button, 0, Qt.AlignmentFlag.AlignLeft)
        columns.addWidget(files, 1)

        settings = QFrame()
        settings.setObjectName("panel")
        settings_box = QVBoxLayout(settings)
        settings_box.setContentsMargins(12, 12, 12, 12)
        settings_box.addWidget(label("Folder settings", "section"))
        scroll = QScrollArea()
        self.settings_scroll = scroll
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        inner.setObjectName("folderOptions")
        options = QVBoxLayout(inner)
        options.setContentsMargins(0, 0, 5, 0)
        options.setSpacing(9)
        def field(title, control):
            options.addWidget(label(title))
            options.addWidget(control)
        self.operation = QComboBox()
        self.operation.addItems(["Inspect sizes", "Flatten one level"])
        field("Operation", self.operation)
        self.size_panel = QWidget()
        size_options = QVBoxLayout(self.size_panel)
        size_options.setContentsMargins(0, 0, 0, 0)
        size_options.setSpacing(9)
        self.minimum, self.maximum = SizeBox(), SizeBox()
        for control in (self.minimum, self.maximum):
            control.setRange(0, 1_000_000_000)
            control.setDecimals(3)
        self.maximum.setSpecialValueText("No limit")
        for title, control in (("Minimum file size", self.minimum), ("Maximum file size", self.maximum)):
            control.setAccessibleName(title + " in MiB")
            size_options.addWidget(label(title))
            row = QHBoxLayout()
            row.setSpacing(7)
            row.addWidget(control, 1)
            unit = label("MiB", "muted")
            unit.setWordWrap(False)
            row.addWidget(unit)
            size_options.addLayout(row)
        self.size_warning = label("Maximum size must be at least the minimum, or 0 for no limit.", "warning")
        self.size_warning.setAccessibleName("Invalid size range")
        self.size_warning.hide()
        size_options.addWidget(self.size_warning)
        options.addWidget(self.size_panel)
        self.depth = QSpinBox()
        self.depth.setRange(0, 9999)
        self.depth.setSpecialValueText("All depths")
        self.depth.setValue(3)
        self.depth.setToolTip("Display depth only. Sizes always include every scanned descendant.")
        field("Display depth", self.depth)
        self.directories = QCheckBox("Folders only")
        options.addWidget(wrapped_check(self.directories))
        self.flatten_panel = QWidget()
        flatten_options = QVBoxLayout(self.flatten_panel)
        flatten_options.setContentsMargins(0, 0, 0, 0)
        flatten_options.setSpacing(9)
        flatten_options.addWidget(label("Flatten scope"))
        self.scope = QComboBox()
        self.scope.addItems(["All parent folders", "Selected parents"])
        self.scope.setToolTip("Inspector filters do not limit this operation. Choose Selected to use the highlighted immediate folders inside the root.")
        flatten_options.addWidget(self.scope)
        flatten_options.addWidget(label("If destination exists"))
        self.collision = QComboBox()
        self.collision.addItem("Skip", "skip")
        self.collision.addItem("Generate unique name", "unique")
        flatten_options.addWidget(self.collision)
        self.loose = QCheckBox("Move loose files too")
        self.loose.setToolTip("Also move files directly inside each selected parent. Otherwise those files stay while eligible subfolders move. Files already in the root stay there.")
        self.remove_empty = QCheckBox("Remove empty parents")
        self.remove_empty.setChecked(True)
        self.remove_empty.setToolTip("Only affected parents are considered. A nonrecursive removal keeps any parent containing remaining or newly arrived entries.")
        flatten_options.addWidget(wrapped_check(self.loose))
        flatten_options.addWidget(wrapped_check(self.remove_empty))
        options.insertWidget(2, self.flatten_panel)
        self.preview_button = button("Preview flatten", self.preview)
        self.queue_button = button("Add to queue", lambda: self.apply(queued=True))
        options.insertWidget(3, self.queue_button)
        self.undo_button = button("Preview undo", self.preview_undo)
        options.insertWidget(4, self.undo_button)
        options.addStretch()
        scroll.setWidget(inner)
        from .controls import SettingsWheelGuard
        self.wheel_guard = SettingsWheelGuard(scroll)
        for control in inner.findChildren(QComboBox):
            control.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            control.setMinimumContentsLength(8)
        for control_type in (QComboBox, QAbstractSpinBox):
            for control in inner.findChildren(control_type):
                control.setMinimumWidth(0)
                control.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
                control.installEventFilter(self.wheel_guard)
        settings_box.addWidget(scroll, 1)
        columns.addWidget(settings)
        main.addLayout(columns, 1)
        actions = QHBoxLayout()
        self.status = label("", "muted")
        self.status.setAccessibleName("Folder status")
        actions.addWidget(self.status, 1)
        self.cancel_button = button("Cancel", self.cancel)
        self.run_button = button("Apply flatten", self.apply, "primary")
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.preview_button)
        actions.addWidget(self.run_button)
        main.addLayout(actions)
        self.inspector=QWidget()
        inspector=QVBoxLayout(self.inspector)
        inspector.setContentsMargins(0,0,0,0)
        inspector.setSpacing(10)
        columns.removeWidget(settings)
        inspector.addWidget(settings,1)
        actions.removeWidget(self.run_button)
        self.run_button.setMinimumHeight(42)
        inspector.addWidget(self.run_button)
        options.removeWidget(self.queue_button)
        actions.removeWidget(self.preview_button)
        secondary=QHBoxLayout()
        secondary.setSpacing(8)
        secondary.addWidget(self.queue_button,1)
        secondary.addWidget(self.preview_button,1)
        inspector.addLayout(secondary)
        self.inspector.setFixedWidth(320)
        columns.addWidget(self.inspector)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        main.addWidget(self.progress)
        result_page=QWidget()
        result_layout=QVBoxLayout(result_page)
        result_layout.setContentsMargins(8,10,8,8)
        self.activity_title=label('')
        self.activity_title.setObjectName('activityTitle')
        self.activity_counts=label('','muted')
        main.removeWidget(self.progress)
        result_layout.addWidget(self.activity_title)
        result_layout.addWidget(self.progress)
        result_layout.addWidget(self.activity_counts)
        self.tabs.removeTab(2)
        result_layout.addWidget(self.results_table,1)
        self.results_table.show()
        self.tabs.addTab(result_page,'Results')
        inputs=QWidget()
        input_layout=QVBoxLayout(inputs)
        input_layout.setContentsMargins(0,8,0,0)
        for row in (path_row,tools):
            contents.removeItem(row)
            row.setParent(None)
            input_layout.addLayout(row)
        self.tabs.removeTab(0)
        input_layout.addWidget(self.tree,1)
        self.tree.show()
        self.tabs.insertTab(0,inputs,'Folders')
        self.tabs.setCurrentIndex(0)
        from . import __version__
        self.filter_timer = QTimer(self)
        self.filter_timer.setSingleShot(True)
        self.filter_timer.setInterval(180)
        self.filter_timer.timeout.connect(self.refilter)
        for control in (self.minimum, self.maximum, self.depth):
            control.valueChanged.connect(self.filter_changed)
        self.directories.toggled.connect(self.filter_changed)
        self.operation.currentIndexChanged.connect(self.mode_changed)
        for control in (self.loose, self.remove_empty):
            control.toggled.connect(self.invalidate_plan)
        self.scope.currentIndexChanged.connect(self.invalidate_plan)
        self.collision.currentIndexChanged.connect(self.invalidate_plan)
        self.tree.selectionModel().selectionChanged.connect(self.selection_changed)
        self.mode_changed()
        if hasattr(host,'preferences'):host.preferences.install_folder(self)

    def resizeEvent(self,event):
        if hasattr(self,'inspector'):
            self.inspector.setFixedWidth(320 if self.width()>=960 else 280 if self.width()>=760 else 250)
        super().resizeEvent(event)

    def mode_changed(self, *args):
        flatten = self.operation.currentIndex() == 1
        self.size_panel.setVisible(not flatten)
        self.flatten_panel.setVisible(flatten)
        self.preview_button.setVisible(flatten)
        self.queue_button.setVisible(flatten)
        self.run_button.setVisible(flatten or bool(self.plan and self.plan.original_job))
        self.invalidate_plan()
        self.filter_changed()

    def update_controls(self):
        busy = self.host.busy
        for control in (self.root, self.open_button, self.refresh_button, self.minimum,
                        self.maximum, self.depth, self.directories, self.operation, self.clear_button,
                        self.flatten_panel):
            control.setEnabled(not busy)
        self.preview_button.setEnabled(not busy and self.snapshot is not None and self.snapshot.complete)
        self.run_button.setEnabled(not busy and self.plan is not None and self.plan.can_apply)
        self.queue_button.setEnabled(not busy and self.plan is not None and self.plan.can_apply)
        self.undo_button.setEnabled(not busy and self.result_undoable)
        self.undo_button.setVisible(self.result_undoable)
        self.cancel_button.setEnabled(busy)
        self.cancel_button.setVisible(busy)
        self.tree.setEnabled(not busy)

    def clear(self):
        if self.host.busy:
            return
        self.root.clear()
        self.discard_snapshot()
        self.result_job = None
        self.result_undoable = False
        self.results_model.replace([], [])
        self.tabs.setCurrentIndex(0)
        self.summary.setText("No folder scanned")
        self.summary.setToolTip("Size filters apply to inspection only. Flattening moves whole folders.")
        self.status.clear()
        self.issues_button.hide()
        self.host.settings.remove("folder_root")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.update_controls()

    def invalidate_plan(self, *args):
        if self.worker is not None:
            return
        self.plan = None
        self.run_button.setText("Apply flatten")
        self.run_button.setVisible(self.operation.currentIndex() == 1)
        self.queue_button.setVisible(self.operation.currentIndex() == 1)
        self.moves_model.replace([], [])
        self.before_model.set_view(None)
        self.after_model.set_view(None)
        self.update_controls()

    def discard_snapshot(self, *args):
        self.filter_timer.stop()
        self.snapshot = None
        self.tree_model.set_view(None)
        self.issues_button.hide()
        self.summary.setText("Root changed; scan this folder to inspect it.")
        self.invalidate_plan()

    def choose_folder(self):
        if self.host.busy:
            return
        path = QFileDialog.getExistingDirectory(self, "Choose root folder", self.root.text())
        if path:
            self.open_paths([path])

    def open_paths(self, paths):
        if self.host.busy:
            return False
        if len(paths) != 1 or not Path(paths[0]).is_dir():
            self.status.setText("Open or drop one root folder at a time.")
            return False
        self.root.setText(str(Path(paths[0]).absolute()))
        self.scan()
        return True

    def options(self):
        inspect = self.operation.currentIndex() == 0
        filtered = inspect and (self.minimum.value() > 0 or self.maximum.value() > 0)
        return dict(minimum=round(self.minimum.value() * 1024 ** 2) if inspect else 0,
                    maximum=round(self.maximum.value() * 1024 ** 2) if inspect and self.maximum.value() else None,
                    directories_only=self.directories.isChecked(), depth=self.depth.value(),
                    sort=("name", "matching" if filtered else "size", "files", "depth", "status")[self.sort_column],
                    descending=self.sort_descending)

    def range_valid(self):
        invalid = (self.operation.currentIndex() == 0 and self.maximum.value() > 0
                   and self.maximum.value() < self.minimum.value())
        self.size_warning.setVisible(invalid)
        return not invalid

    def sort_clicked(self, column):
        if self.host.busy:
            return
        self.sort_descending = not self.sort_descending if column == self.sort_column else column in (1, 2)
        self.sort_column = column
        self.tree.header().setSortIndicator(column, Qt.SortOrder.DescendingOrder if self.sort_descending else Qt.SortOrder.AscendingOrder)
        self.filter_changed()

    def launch(self, kind, work, message):
        if self.host.busy or self.host._closing:
            return False
        self.filter_timer.stop()
        self.worker = FolderWorker(kind, work, self)
        self.worker.finished.connect(self.finished)
        self.worker.progress.connect(lambda n: self.status.setText(f"Scanning folders… {n:,} entries found."))
        self.progress.setRange(0, 0)
        self.status.setText(message)
        self.host.update_controls()
        self.worker.start()
        return True

    def scan(self):
        if self.host.busy or not self.root.text().strip():
            return
        if not self.range_valid():
            self.status.setText(self.size_warning.text())
            return
        root, options = self.root.text().strip(), self.options()
        self.discard_snapshot()
        self.summary.setText("Scanning… totals are not complete yet.")
        self.tabs.setCurrentIndex(0)
        def work(cancelled, progress):
            snapshot = scan_folder(root, cancelled, progress)
            # A cancelled scan still exposes clearly labelled partial totals.
            return snapshot, folder_view(snapshot, **options)
        self.launch("scan", work, "Scanning folders and calculating sizes…")

    def filter_changed(self, *args):
        self.invalidate_plan()
        self.filter_timer.stop()
        if not self.range_valid():
            self.status.setText(self.size_warning.text())
            return
        if self.status.text() == self.size_warning.text():
            self.status.clear()
        if self.snapshot is not None:
            self.filter_timer.start()

    def refilter(self):
        if self.snapshot is None or self.host.busy:
            return
        if not self.range_valid():
            return
        snapshot, options = self.snapshot, self.options()
        self.launch("filter", lambda cancelled, progress: folder_view(snapshot, **options, cancelled=cancelled),
                    "Filtering cached sizes…")

    def selection_changed(self, *args):
        if self.scope.currentIndex() == 1:
            self.invalidate_plan()

    def preview(self):
        if self.host.busy or self.snapshot is None:
            return
        snapshot = self.snapshot
        selected = None
        if self.scope.currentIndex() == 1:
            selected = [snapshot.nodes[i.internalId() - 1].path
                        for i in self.tree.selectionModel().selectedRows(0)]
        options = dict(include_files=self.loose.isChecked(), collision=self.collision.currentData(),
                       remove_empty=self.remove_empty.isChecked(), selected=selected)
        view_options = self.options()
        self.invalidate_plan()
        def work(cancelled, progress):
            plan = plan_flatten(snapshot, **options, cancelled=cancelled)
            after = projected_snapshot(snapshot, plan, cancelled) if plan.can_apply else None
            return (plan, folder_view(snapshot, cancelled=cancelled),
                    folder_view(after, cancelled=cancelled) if after else None,
                    folder_view(snapshot, **view_options, cancelled=cancelled))
        self.launch("preview", work, "Checking one-level moves and destination conflicts…")

    def preview_undo(self, job_id=None):
        # clicked(bool) is not a job identifier.
        job_id = job_id if isinstance(job_id, str) else self.result_job
        if self.host.busy or not job_id:
            return False
        try:
            queue = self.host.ensure_jobs()
            if not queue.can_start_immediately:
                raise ValueError("Pause Queue and wait for active work before previewing undo.")
            directory = queue.directory
            self.invalidate_plan()
            def work(cancelled, progress):
                store = Store(directory)
                try:
                    return plan_folder_undo(store, job_id, cancelled), None, None, None
                finally:
                    store.close()
            return self.launch("preview", work, "Checking moved contents and original locations for undo…")
        except Exception as error:
            self.status.setText(str(error))
            return False

    def set_tree(self, model, view, widget):
        model.set_view(view)
        if view:
            widget.expand(model.index(0, 0))

    def show_view(self, view):
        self.set_tree(self.tree_model, view, self.tree)
        snapshot = view.snapshot
        partial = "Partial snapshot" if not snapshot.complete else "Snapshot"
        links = sum(n.kind == "Link" for n in snapshot.nodes)
        text = f"{partial}: {snapshot.nodes[0].files:,} files · {format_size(snapshot.nodes[0].total)} total"
        if view.min_bytes or view.max_bytes is not None:
            text += f" · {view.matching_files[0]:,} matching files · {format_size(view.matching_bytes[0])} matching"
        self.summary.setText(text
                             + (f" · {links} links excluded" if links else ""))
        self.issues_button.setVisible(bool(snapshot.issues))
        self.issues_button.setText(f"Scan issues ({len(snapshot.issues)})")

    def finished(self):
        worker, self.worker = self.worker, None
        self.progress.setRange(0, 100)
        self.progress.setValue(0 if worker.error else 100)
        try:
            if self.host._closing:
                return
            if worker.error is not None:
                self.status.setText("Cancelled. No files changed." if isinstance(worker.error, Cancelled) else str(worker.error))
                if worker.kind in ("filter", "preview"):
                    self.invalidate_plan()
            elif worker.kind == "scan":
                self.snapshot, view = worker.result
                self.show_view(view)
                self.host.settings.setValue("folder_root", self.snapshot.root)
                self.status.setText("Scan cancelled; totals are partial. Refresh before flattening." if self.snapshot.cancelled else
                                    "Some folders could not be fully scanned. See Scan issues; flattening is blocked." if not self.snapshot.complete else
                                    "Folder sizes ready. Filters use cached totals; Refresh reads the folder again.")
            elif worker.kind == "filter":
                self.show_view(worker.result)
                self.status.setText("Cached view updated. Display depth does not limit size totals.")
            elif worker.kind == "preview":
                self.plan, before, after, current = worker.result
                if current is not None:
                    # A tree reset can clear a Selected-parents selection. Keep
                    # the just-built frozen preview while updating display rows.
                    selection = self.tree.selectionModel()
                    blocked = selection.blockSignals(True)
                    try:
                        self.show_view(current)
                    finally:
                        selection.blockSignals(blocked)
                if self.plan.original_job:
                    self.root.setText(self.plan.root)
                    if self.snapshot and self.snapshot.root != self.plan.root:
                        self.snapshot = None
                        self.tree_model.set_view(None)
                        self.summary.setText("Undo preview uses saved history. Refresh to inspect this root.")
                root = Path(self.plan.root)
                rows = [(str(Path(i.source).relative_to(root)), str(Path(i.output).relative_to(root)) if i.output else "—", i.status, i.reason)
                        for i in self.plan.items]
                self.moves_model.replace(list(range(len(rows))), rows,
                                         [f"{i.source}\n{i.output or ''}\n{i.reason}" for i in self.plan.items])
                self.set_tree(self.before_model, before, self.before_tree)
                self.set_tree(self.after_model, after, self.after_tree)
                self.preview_tabs.setTabEnabled(1, before is not None and after is not None)
                self.preview_tabs.setCurrentIndex(0)
                self.tabs.setCurrentIndex(1)
                self.run_button.setText("Apply undo" if self.plan.original_job else "Apply flatten")
                self.run_button.show()
                self.queue_button.show()
                counts = Counter(i.status for i in self.plan.items)
                self.status.setText(f"Preview: {counts['Ready']} moves, {counts['Skipped']} skipped, {counts['Conflict']} conflicts. "
                                    + ("Review, then Apply undo." if self.plan.original_job else "Review before applying. No files changed."))
        finally:
            worker.deleteLater()
            self.host.update_controls()
            if self.host._closing:
                QTimer.singleShot(0, self.host.close)

    def apply(self, queued=False):
        if self.host.busy or self.plan is None or not self.plan.can_apply:
            return
        try:
            queue = self.host.ensure_jobs()
            if not queued and not queue.can_start_immediately:
                raise ValueError("Pause Queue and wait for active work before applying folder moves.")
            plan = self.plan
            job_id = (queue.store.enqueue_undo(plan.original_job, plan.specs) if plan.original_job else
                      queue.store.enqueue("Flatten folders", "flatten", plan.specs))
            self.invalidate_plan()
            self.result_undoable = False
            self.result_job = job_id
            if queued:
                queue.refresh(job_id)
                self.status.setText("Folder moves queued. Start Queue when ready; the saved plan will be checked again.")
                return
            self.status.setText("Restoring original locations…" if plan.original_job else "Moving children up one level…")
            queue.start_single(job_id)
        except Exception as error:
            self.status.setText(str(error))

    def show_results(self, store, job_id, focus=True):
        self.filter_timer.stop()
        self.snapshot = None
        self.tree_model.set_view(None)
        self.invalidate_plan()
        self.result_job = job_id
        items = store.items(job_id)
        rows, tips = [], []
        for item in items:
            spec = json.loads(item["spec"])
            self.root.setText(spec["root"])
            rows.append((spec["source"], spec["output"] or "—", item["state"], item["message"]))
            tips.append(f"{spec['source']}\n{spec['output'] or ''}\n{item['message']}")
        self.results_model.replace([i["id"] for i in items], rows, tips)
        counts = Counter(i["state"] for i in items)
        original = store.original_job(job_id)
        self.result_undoable = (not original and not store.undo_job(job_id) and bool(counts["succeeded"])
                                and all(i["state"] in ("succeeded", "skipped") for i in items))
        cleanup = Counter(r["state"] for r in cleanup_rows(store, original or job_id))
        incomplete = sum(counts[s] for s in ("failed", "cancelled", "interrupted", "queued"))
        self.status.setText(f"{counts['succeeded']} moves completed; {counts['skipped']} skipped; {incomplete} incomplete. "
                            + ("Open Queue to inspect or retry." if incomplete else "Original locations restored." if original else "Preview undo is available."))
        self.summary.setText(f"{cleanup['removed']} empty parents removed · {cleanup['kept']} parents kept · "
                             f"{cleanup['recreated']} recreated for undo. Refresh to inspect current contents.")
        self.summary.setToolTip("\n".join(f"{r['path']}: {r['message'] or r['state']}" for r in cleanup_rows(store, original or job_id)))
        self.progress.setRange(0, 100)
        self.progress.setValue(0 if incomplete else 100)
        self.activity_title.setText('Incomplete' if incomplete else 'Completed')
        self.activity_counts.setText(f"{counts['succeeded']} completed · {counts['skipped']} skipped · {incomplete} incomplete")
        if focus:self.tabs.setCurrentIndex(2)
        self.update_controls()

    def show_issues(self):
        text = "\n".join(self.snapshot.issues) if self.snapshot and self.snapshot.issues else "No scan issues recorded."
        self.host._show_text("Folder scan details", text)

    def reveal(self, *args):
        index = self.tree.currentIndex()
        path = self.root.text().strip()
        if self.snapshot and index.isValid():
            path = self.snapshot.nodes[index.internalId() - 1].path
        if not path or not Path(path).exists():
            self.status.setText("Location is no longer available. Refresh the folder.")
            return
        if sys.platform == "win32":
            # Pass an argument vector; paths never enter a shell command.
            try:
                subprocess.Popen(["explorer.exe", "/select,", str(Path(path))],
                                 creationflags=subprocess.CREATE_NO_WINDOW)
            except OSError as error:
                self.status.setText(f"Could not reveal this location: {error}")
        else:
            folder = path if Path(path).is_dir() else str(Path(path).parent)
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def cancel(self):
        if self.worker is not None:
            self.worker.requestInterruption()
            self.status.setText("Cancel requested…")
            return
        queue = self.host.jobs_dialog
        if queue and queue.worker:
            self.cancelled_job=queue.worker.job_id
            queue.worker.cancelled.set()
            self.status.setText("Stopping after the current atomic move. Completed moves remain recorded."
                                if queue.worker.adapter == "flatten" else
                                "Cancel requested for the active job. Open Queue for details.")

    def shutdown(self):
        self.filter_timer.stop()
        if self.worker is not None:
            self.worker.requestInterruption()
            return False
        return True
