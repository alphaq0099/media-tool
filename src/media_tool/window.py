from pathlib import Path
import os
import time

from PySide6.QtCore import Qt, QSettings, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QFrame, QLabel, QPushButton, QCheckBox,
    QVBoxLayout, QHBoxLayout, QComboBox, QLineEdit, QTableView, QHeaderView,
    QFileDialog, QProgressBar, QButtonGroup, QAbstractItemView, QMessageBox,
    QDialog, QPlainTextEdit, QMenu, QToolButton, QStackedWidget,
)
from .model import FileModel
from .services import ScanWorker, SortWorker, ToolProbe
from .theme import apply_theme, THEMES, ThemeRefresh
from .planning_ui import PlanningUI
from .navigation import ICON_SIZE, navigation_icon
from .flow_layout import FlowLayout
from . import __version__

def label(text, name=None, wrap=False):
    item = QLabel(text)
    item.setTextFormat(Qt.TextFormat.PlainText)
    item.setWordWrap(wrap)
    if name:
        item.setObjectName(name)
    return item

def button(text, action=None, name=None):
    item = QPushButton(text)
    item.setCursor(Qt.CursorShape.PointingHandCursor)
    if name:
        item.setObjectName(name)
    if action:
        item.clicked.connect(action)
    return item


class StatusLabel(QLabel):
    def setText(self, text):
        super().setText(text)
        self.setToolTip(text)
        host = self.window()
        if hasattr(host, 'visual'):
            host.visual.refresh()

    def clear(self):
        self.setText("")

class Workspace(PlanningUI, QMainWindow):
    painted = Signal()
    scan_complete = Signal()
    plan_complete = Signal()
    theme_changed = Signal(str)

    def __init__(self, settings=None):
        super().__init__()
        self.setWindowTitle("Media Tool")
        from .branding import application_icon
        self.setWindowIcon(application_icon())
        self.resize(1180, 740)
        self.setMinimumSize(880, 610)
        self.setAcceptDrops(True)
        self.settings = settings if settings is not None else QSettings("MediaTool", "Workspace")
        self.worker = None
        self.issues = []
        self.probe = ToolProbe(self)
        self.probe.result.connect(self._probe_result)
        self.pending_probes = []
        self._closing = False
        self._paint_announced = False
        self.tool = "Images"
        self.jobs_dialog = None
        self.folders_page = None
        self.dependencies_page = None
        self._build()
        from .interaction import InteractionCursors
        self.interaction_cursors=InteractionCursors(self)
        self.install_planning()
        from .visual_ui import VisualWorkspace
        self.visual=VisualWorkspace(self)
        from .input_types import InputTypes
        self.input_types=InputTypes(self)
        self.set_theme(str(self.settings.value("theme", "dark")), persist=False)
        self._tool_changed("Images")
        from .preferences import ToolPreferences
        self.preferences=ToolPreferences(self)
        self.preferences.enter()
        open_action = QAction("Add files", self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self.add_files)
        self.addAction(open_action)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._finish_probe_checks)

    def _build(self):
        root = QWidget()
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.setCentralWidget(root)
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(184)
        nav = QVBoxLayout(side)
        self.sidebar_layout = nav
        nav.setContentsMargins(16, 27, 16, 20)
        nav.setSpacing(8)
        nav.addWidget(label("Media Tool", "brand"))
        nav.addSpacing(26)
        self.nav_group = QButtonGroup(self)
        for title in ("Images", "Video", "Audio", "Rename", "Archives", "Documents", "Folders"):
            control = button(title, name="nav")
            control.setCheckable(True)
            control.setChecked(title == "Images")
            control.clicked.connect(lambda checked=False, t=title: self._tool_changed(t))
            self.nav_group.addButton(control)
            nav.addWidget(control)
        nav.addSpacing(12)
        self.jobs_button = button("Queue", self.open_jobs, name="nav")
        self.jobs_button.setCheckable(True)
        self.nav_group.addButton(self.jobs_button)
        nav.addWidget(self.jobs_button)
        nav.addStretch()
        outer.addWidget(side)

        workspace = QWidget()
        self.main_workspace = workspace
        workspace.setObjectName("workspace")
        main = QVBoxLayout(workspace)
        main.setContentsMargins(12, 8, 12, 12)
        main.setSpacing(12)
        headline = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(4)
        self.heading = label("Convert images", "heading")
        self.subtitle = label("", "muted", True)
        titles.addWidget(self.heading)
        titles.addWidget(self.subtitle)
        headline.addLayout(titles, 1)
        main.addLayout(headline)
        columns = QHBoxLayout()
        self.columns = columns
        columns.setSpacing(18)
        files_panel = QFrame()
        files_panel.setObjectName("panel")
        file_layout = QVBoxLayout(files_panel)
        self.file_layout = file_layout
        file_layout.setContentsMargins(18, 18, 18, 16)
        file_layout.setSpacing(12)
        toolbar = FlowLayout()
        self.file_toolbar = toolbar
        self.add_button = QToolButton()
        self.add_button.setObjectName("openButton")
        self.add_button.setText("Open")
        self.add_button.setMinimumWidth(78)
        self.add_button.setMinimumHeight(38)
        self.add_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.add_button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.add_button.setToolTip("Open files. Use the arrow to open a folder.")
        self.add_button.clicked.connect(self.add_files)
        open_menu = QMenu(self.add_button)
        self.open_files_action = open_menu.addAction("Files…", self.add_files)
        self.open_folder_action = open_menu.addAction("Folder…", self.add_folder)
        self.add_button.setMenu(open_menu)
        toolbar.addWidget(self.add_button)
        self.recursive = QCheckBox("Subfolders")
        self.recursive.setToolTip("Also scan subfolders when opening a folder. Links are skipped.")
        toolbar.addWidget(self.recursive)
        toolbar.addStretch()
        self.clear_button = button("Clear list", self.clear_files)
        toolbar.addWidget(self.clear_button)
        file_layout.addLayout(toolbar)
        self.model = FileModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setAccessibleName("Batch files")
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(43)
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for i in (1, 2, 3):
            self.table.horizontalHeader().setSectionResizeMode(i, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(1, 63)
        self.table.setColumnWidth(2, 85)
        self.table.setColumnWidth(3, 73)
        file_layout.addWidget(self.table, 1)
        totals = QHBoxLayout()
        self.file_totals = totals
        self.count_label = label("0 files", "muted")
        totals.addWidget(self.count_label, 1)
        self.issues_button = button("Scan details", self.show_issues)
        self.issues_button.setToolTip("Shows errors and skipped locations from the last file scan, such as unreadable folders or links.")
        self.issues_button.setAccessibleDescription(self.issues_button.toolTip())
        file_layout.addLayout(totals)
        columns.addWidget(files_panel, 1)

        settings_panel = QFrame()
        self.settings_panel = settings_panel
        settings_panel.setObjectName("panel")
        settings_panel.setFixedWidth(237)
        settings_layout = QVBoxLayout(settings_panel)
        self.settings_layout = settings_layout
        settings_layout.setContentsMargins(18, 18, 18, 18)
        settings_layout.setSpacing(9)
        settings_layout.addWidget(label("Output settings", "section"))
        self.option_title = label("Format")
        settings_layout.addWidget(self.option_title)
        self.format = QComboBox()
        settings_layout.addWidget(self.format)
        self.detail_title = label("Quality")
        settings_layout.addWidget(self.detail_title)
        self.detail = QComboBox()
        settings_layout.addWidget(self.detail)
        self.destination_label = label("Destination")
        settings_layout.addWidget(self.destination_label)
        self.destination = QLineEdit()
        self.destination.setReadOnly(True)
        self.destination.setPlaceholderText("Choose when processing is available")
        settings_layout.addWidget(self.destination)
        self.keep = QCheckBox("Keep originals")
        self.keep.setChecked(True)
        self.keep.setEnabled(True)
        self.keep.setToolTip("Uncheck to move each original to the Recycle Bin after its output is verified and saved. Failed or cancelled conversions keep the original.")
        settings_layout.addWidget(self.keep)
        self.phase_hint = label("", "muted", True)
        settings_layout.addWidget(self.phase_hint)
        settings_layout.addStretch()
        self.tools_button = button("Check media tools", self.check_tools)
        self.tools_button.setParent(side)
        self.tools_button.hide()
        diagnostics = button("Tools")
        self.diagnostics_button = diagnostics
        diagnostics_menu = QMenu(diagnostics)
        self.dependencies_action=diagnostics_menu.addAction('Dependencies…',self.show_dependencies)
        diagnostics_menu.addSeparator()
        diagnostics_menu.addAction("Check media tools", self.check_tools)
        diagnostics.setMenu(diagnostics_menu)
        diagnostics.setProperty("hasMenu", True)
        columns.addWidget(settings_panel)
        main.addLayout(columns, 1)
        actions = QHBoxLayout()
        self.actions = actions
        self.status = StatusLabel("")
        self.status.setObjectName("muted")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setMaximumHeight(48)
        self.status.setAccessibleName("Status")
        actions.addWidget(self.status, 1)
        self.cancel_button = button("Cancel scan", self.cancel_scan)
        self.cancel_button.setMinimumWidth(104)
        self.cancel_button.setEnabled(False)
        actions.addWidget(self.cancel_button)
        self.run_button = button("Processing not available", name="primary")
        self.run_button.setEnabled(False)
        actions.addWidget(self.run_button)
        main.addLayout(actions)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        main.addWidget(self.progress)
        footer = QHBoxLayout()
        nav.addWidget(label(__version__, "muted"))
        self.theme_button = button("Light / Dark", self.toggle_theme)
        self.theme_button.setCheckable(True)
        self.theme_button.setAccessibleName("Dark mode")
        nav.addWidget(diagnostics)
        nav.addSpacing(10)
        nav.addWidget(self.theme_button)
        main.addLayout(footer)
        self.workspace_stack = QStackedWidget()
        self.workspace_stack.addWidget(workspace)
        outer.addWidget(self.workspace_stack, 1)

    @property
    def folders_active(self):
        return self.folders_page is not None and self.workspace_stack.currentWidget() == self.folders_page

    def show_folders(self):
        if self.folders_page is None:
            from .folder_ui import FolderWorkspace
            self.folders_page = FolderWorkspace(self)
            self.workspace_stack.addWidget(self.folders_page)
        self.workspace_stack.setCurrentWidget(self.folders_page)
        for control in self.nav_group.buttons():
            control.setChecked(control.text() == "Folders")
        self.folders_page.update_controls()
        queue=self.jobs_dialog
        active=bool(queue and queue.worker and queue.worker.adapter=='flatten')
        self.folders_page.tabs.setCurrentIndex(2 if active else 0)
        return self.folders_page

    def apply_folder_results(self, items):
        """Refresh cached intake paths without scanning directories on the GUI thread."""
        from dataclasses import replace
        import json
        moves = {os.path.normcase(json.loads(i["spec"])["source"]): json.loads(i["spec"])["output"]
                 for i in items if i["state"] == "succeeded"}
        updated = []
        for record in self.model.items:
            path = Path(record.path)
            for candidate in (path, *path.parents):
                output = moves.get(os.path.normcase(str(candidate)))
                if output:
                    target = Path(output) / path.relative_to(candidate)
                    record = replace(record, path=str(target), name=target.name)
                    break
            updated.append(record)
        self.invalidate_plan()
        if updated != self.model.items:
            self.model.replace_order(sorted(updated, key=lambda r: r.sort_key))

    def ensure_jobs(self):
        if self._closing:
            raise RuntimeError("The app is closing.")
        if self.jobs_dialog is None:
            from .jobs_ui import QueuePage
            self.jobs_dialog = QueuePage(self)
            self.workspace_stack.addWidget(self.jobs_dialog)
        return self.jobs_dialog

    def open_jobs(self):
        if self._closing:
            return
        try:
            queue = self.ensure_jobs()
            queue.show()
        except Exception as error:
            self._show_text("Queue unavailable", str(error))

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._paint_announced:
            self._paint_announced = True
            QTimer.singleShot(0, self.painted.emit)

    def set_theme(self, name, persist=True):
        self.setUpdatesEnabled(False)
        try:
            self.theme = name if name in ("light", "dark") else "light"
            self.setProperty('mediaTheme', self.theme)
            apply_theme(QApplication.instance(), self.theme)
            colors = THEMES[self.theme]
            # The application stylesheet stays constant; palette changes carry colors.
            self.setPalette(QApplication.instance().palette())
            if not hasattr(self, '_theme_refresh'):
                self._theme_refresh = ThemeRefresh(self)
            self._theme_refresh.refresh(self)
            self.theme_button.setChecked(self.theme == "dark")
            for control in (*self.nav_group.buttons(), self.jobs_button):
                control.setIcon(navigation_icon(control.text(), colors["accent"]))
                control.setIconSize(ICON_SIZE)
            self.theme_button.setText("Dark" if self.theme == "dark" else "Light")
            self.theme_button.setAccessibleName('Current theme: '+self.theme)
            if persist:
                self.settings.setValue("theme", self.theme)
                self.settings.sync()
                if self.settings.status() != QSettings.Status.NoError:
                    self.status.setText("Theme changed, but the preference could not be saved.")
            self.theme_changed.emit(self.theme)
        finally:
            self.setUpdatesEnabled(True)

    def toggle_theme(self):
        self.set_theme("light" if self.theme == "dark" else "dark")

    def _base_tool_changed(self, title):
        self.tool = title
        for control in self.nav_group.buttons():
            control.setChecked(control.text() == title)
        self.format.clear()
        self.detail.clear()
        self.option_title.setText("Naming style" if title == "Rename" else "Container" if title == "Video" else "Format")
        self.detail_title.setText("Numbering" if title == "Rename" else "Quality")
        self.detail.setToolTip("Low favors smaller files; Best retains more detail but is still lossy. Quality sets a target, not the source bitrate or a fixed file size. Any quality works with any speed."
                               if title == "Video" else "")
        if title == "Images":
            self.heading.setText("Convert & resize images")
            self.format.addItems(["JPEG", "PNG", "WebP"])
            self.detail.addItems(["Balanced", "High quality"])
            self.phase_hint.setText("Image conversion arrives in phase 4. These settings are a preview.")
        elif title == "Video":
            self.heading.setText("Convert & resize videos")
            self.format.addItems(["MP4", "MKV", "WebM"])
            for label, value in (("Low", 28), ("Balanced", 23), ("High", 20), ("Best", 18)):
                self.detail.addItem(label, value)
            self.detail.setCurrentIndex(self.detail.findData(23))
            self.phase_hint.setText("Video conversion arrives in phase 5. These settings are a preview.")
        elif title == "Documents":
            self.heading.setText("Work with documents")
            self.format.addItems(["PDF"])
        elif title == "Audio":
            self.heading.setText("Work with audio")
            self.format.addItems(["MP3", "M4A / AAC", "FLAC", "WAV", "Opus"])
        elif title == "Archives":
            self.heading.setText("Create archives")
            self.format.addItems(["7z", "ZIP", "CBZ"])
        else:
            self.heading.setText("Rename files")
            self.format.addItems(["Sequential prefix", "Random names", "Clean separators"])
            for text, width in (("1 / one digit", 1), ("01 / two digits", 2),
                                ("001 / three digits", 3), ("0001 / four digits", 4)):
                self.detail.addItem(text, width)
            self.detail.setCurrentIndex(3)
            self.phase_hint.setText("Completed rename batches can be undone from Results.")
        self.subtitle.setText({
            "Images": "Convert formats and resize images.",
            "Video": "Convert, resize, compress or remux videos.",
            "Audio": "Convert, extract, edit, normalize and tag audio.",
            "Documents": "Manage PDF pages, images, text and document protection.",
            "Rename": "Rename files in batches without changing their contents.",
            "Archives": "Pack files and folders into 7z, ZIP or comic archives.",
        }.get(title, "Work with selected files and folders."))
        self.keep.setText("Contents unchanged" if title == "Rename" else "Keep originals")

    def add_files(self):
        if self.folders_active:
            self.folders_page.choose_folder()
            return
        if self.editing_busy:
            return
        from .input_picker import pick_inputs
        paths = pick_inputs(self)
        if paths:
            self.start_scan(paths)

    def add_folder(self):
        if self.folders_active:
            self.folders_page.choose_folder()
            return
        if self.editing_busy:
            return
        path = QFileDialog.getExistingDirectory(self, "Open a folder")
        if path:
            self.start_scan([path])

    @property
    def busy(self):
        queue_worker = self.jobs_dialog.worker if self.jobs_dialog is not None else None
        renaming = queue_worker is not None and queue_worker.adapter in ("rename", "images", "video", "audio", "documents", "archives", "flatten", "workflow")
        return self.editing_busy or renaming

    @property
    def editing_busy(self):
        """Foreground readers lock editing; frozen queue jobs run independently."""
        folder_work = self.folders_page is not None and self.folders_page.worker is not None
        audio_work = hasattr(self, "audio_controls") and self.audio_controls.worker is not None
        document_work = getattr(self,"document_controls",None) is not None and self.document_controls.worker is not None
        return document_work or audio_work or folder_work or self.worker is not None or getattr(self, 'plan_worker', None) is not None or getattr(self, "archive_cleanup_worker", None) is not None

    def apply_rename_results(self, updates):
        mapping = {(spec["source"], spec["identity"][0], spec["identity"][1]): record
                   for spec, record in updates}
        records = [mapping.get((record.path, record.device, record.inode), record) for record in self.model.items]
        if records != self.model.items:
            self.invalidate_plan()
            self.model.replace_order(sorted(records, key=lambda record: record.sort_key))
            self.update_counts()

    def start_scan(self, paths=(), sample_count=0):
        if self.folders_active and not sample_count:
            return self.folders_page.open_paths(paths)
        if self.editing_busy:
            return False
        self.apply_filters()
        self.filter_timer.stop()
        self.invalidate_plan()
        self.tabs.setCurrentIndex(0)
        self.issues.clear()
        self._scan_cancelled = False
        if sample_count:
            self.model.clear()
        excluded = ()
        if self.tool != "Rename" and self.destination.text().strip():
            candidate = Path(self.destination.text().strip()).expanduser()
            if candidate.is_absolute():
                excluded = (str(candidate),)
        self.worker = ScanWorker(paths, self.recursive.isChecked(), sample_count, self,
                                 excluded_paths=excluded, preserve_folders=self.tool == "Archives")
        self.worker.records.connect(self._append)
        self.worker.issue.connect(self.issues.append)
        self.worker.summary.connect(self._scan_summary)
        self.worker.finished.connect(self._scan_finished)
        self.update_controls()
        self.cancel_button.setEnabled(True)
        self.progress.setRange(0, 0)
        self.status.setText("Generating sample rows..." if sample_count else "Listing files... originals are untouched.")
        self.worker.start()
        return True

    def load_samples(self):
        self.start_scan(sample_count=10000)

    def _append(self, records):
        self.model.append(records)
        self.update_counts()

    def _scan_summary(self, count, cancelled):
        if self.tool == "Archives" and (cancelled or self.issues):
            self.archive_intake_incomplete = True
        self._scan_cancelled = cancelled
        verb = "Cancelled" if cancelled else "Finished"
        self.status.setText(f"{verb}: {count:,} files found; {len(self.issues)} scan notices. No files changed.")

    def _scan_finished(self):
        worker = self.worker
        self.worker = None
        if worker is not None:
            worker.deleteLater()
        if not self._closing and not self._scan_cancelled:
            self.worker = SortWorker(self.model.items, self)
            self.worker.ordered.connect(self.model.replace_order)
            self.worker.finished.connect(self._sort_finished)
            self.worker.start()
            return
        self._finish_intake()

    def _sort_finished(self):
        worker = self.worker
        cancelled = worker.isInterruptionRequested()
        self.worker = None
        worker.deleteLater()
        if cancelled:
            self.status.setText("Cancelled ordering. Listed files are unchanged.")
        self._finish_intake()

    def _finish_intake(self):
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        self.cancel_button.setEnabled(False)
        self.apply_filters()
        self.update_controls()
        self.scan_complete.emit()
        if self._closing:
            self.close()

    def cancel_scan(self, background=False):
        cleanup = getattr(self, "archive_cleanup_worker", None)
        if cleanup is not None:
            cleanup.cancelled.set()
            self.status.setText("Stopping source cleanup; remaining originals will be kept.")
        queue_worker = self.jobs_dialog.worker if self.jobs_dialog is not None else None
        if (background or not self.editing_busy) and queue_worker is not None and queue_worker.adapter in ("rename", "images", "video", "audio", "documents", "archives", "workflow"):
            queue_worker.cancelled.set()
            self.cancel_button.setEnabled(False)
            self.status.setText("Cancel requested. Stopping FFmpeg and cleaning up the partial output."
                                if queue_worker.adapter in ("video", "audio") else
                                "Cancel requested. Waiting for the current image decode or encode to return." if queue_worker.adapter == "images" else
                                "Cancel requested. Stopping archive creation and removing owned partial output." if queue_worker.adapter == "archives" else
                                "Cancel requested. The rename batch finishes or restores its original names safely.")
        if background:return
        if self.audio_controls.worker is not None:
            self.audio_controls.worker.requestInterruption()
            self.cancel_button.setEnabled(False)
            self.status.setText("Cancelling track inspection…")
        if getattr(self,"document_controls",None) is not None and self.document_controls.worker is not None:
            self.document_controls.worker.requestInterruption()
            self.cancel_button.setEnabled(False)
        if self.plan_worker:
            self._start_after_plan = False
            self.plan_worker.requestInterruption()
            self.cancel_button.setEnabled(False)
            self.status.setText("Cancelling preview...")
        if self.worker:
            self.worker.requestInterruption()
            self.cancel_button.setEnabled(False)
            self.status.setText("Cancelling discovery...")

    def clear_files(self):
        if not self.editing_busy:
            self.archive_intake_incomplete = False
            self.invalidate_plan()
            self.model.clear()
            self.update_counts()
            self.progress.setValue(0)
            self.status.setText("List cleared. No files changed.")

    def show_issues(self):
        explanation = ("Scan details describes what happened while adding files and folders.\n"
                       "It lists unreadable locations, skipped links and excluded output folders.\n"
                       "It does not report rename results; those are saved in Queue.\n\n")
        self._show_text("Scan details", explanation + ("\n".join(self.issues) if self.issues else "No scan notices."))

    def _show_text(self, title, text):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(620, 320)
        layout = QVBoxLayout(dialog)
        output = QPlainTextEdit()
        output.setReadOnly(True)
        output.setPlainText(text)
        layout.addWidget(output)
        layout.addWidget(button("Close", dialog.accept))
        dialog.exec()

    def show_dependencies(self):
        if self._closing:return
        if self.dependencies_page is None:
            from .dependency_ui import DependencyPage
            self.dependencies_page=DependencyPage(self)
            self.workspace_stack.addWidget(self.dependencies_page)
        page=self.dependencies_page
        if self.workspace_stack.currentWidget() is not page:page.previous=self.workspace_stack.currentWidget()
        self.workspace_stack.setCurrentWidget(page)
        if not page.busy:page.check()

    def check_tools(self):
        if not self.probe.done or self.pending_probes:
            return
        self.tool_results = []
        self.pending_probes = ["ffprobe", "ffmpeg", "7z"]
        self.tools_button.setEnabled(False)
        self._next_probe()

    def _next_probe(self):
        if self.pending_probes:
            tool = self.pending_probes.pop(0)
            self.status.setText(f"Checking {tool}...")
            self.probe.start(tool, ["i"] if tool == "7z" else ["-version"])
        else:
            self.timer.start(0)

    def _probe_result(self, text):
        if self._closing:
            return
        self.tool_results.append(text)
        QTimer.singleShot(0, self._next_probe)

    def _finish_probe_checks(self):
        self.tools_button.setEnabled(True)
        self.status.setText("Tool check complete. Missing tools do not affect file intake.")
        if not self._closing:
            self._show_text("Media tools", "\n\n".join(self.tool_results))

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and not self.editing_busy and any(u.isLocalFile() for u in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if paths and self.start_scan(paths):
            event.acceptProposedAction()

    def closeEvent(self, event):
        if not self._closing and hasattr(self,'preferences'):
            self.preferences.save_current()
            self.preferences.flush()
        self._closing = True
        dependencies_ready=self.dependencies_page is None or self.dependencies_page.shutdown()
        if hasattr(self,'waveform') and not self.waveform.shutdown():
            QTimer.singleShot(50,self.close)
            event.ignore()
            return
        if self.folders_page is not None and not self.folders_page.shutdown():
            QTimer.singleShot(50, self.close)
            event.ignore()
            return
        cleanup = getattr(self, "archive_cleanup_worker", None)
        if cleanup is not None:
            cleanup.cancelled.set()
            QTimer.singleShot(50, self.close)
            event.ignore()
            return
        if not self.resize_controls.shutdown():
            QTimer.singleShot(50, self.close)
            event.ignore()
            return
        comparison = getattr(self, "comparison_dialog", None)
        if comparison is not None:
            comparison.close()
            if comparison.worker is not None:
                QTimer.singleShot(50, self.close)
                event.ignore()
                return
        self.pending_probes.clear()
        self.filter_timer.stop()
        self.timer.stop()
        self.probe.cancel()
        if self.jobs_dialog is not None and not self.jobs_dialog.shutdown():
            QTimer.singleShot(50, self.close)
            event.ignore()
            return
        if self.busy:
            self.cancel_scan()
            event.ignore()
            return
        if self.probe.process.state() != self.probe.process.ProcessState.NotRunning:
            QTimer.singleShot(50, self.close)
            event.ignore()
            return
        if not dependencies_ready:
            QTimer.singleShot(50,self.close)
            event.ignore()
            return
        if hasattr(self,'visual'):self.visual.help_policy.stop()
        event.accept()
