"""Phase-1 workspace behavior. File and output previews are read-only."""
from pathlib import Path
import json
import uuid
from PySide6.QtCore import Qt, QTimer
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QHBoxLayout, QVBoxLayout, QLabel, QLineEdit, QComboBox, QPushButton,
    QFileDialog, QTabWidget, QTableView, QAbstractItemView, QHeaderView,
    QScrollArea, QFrame, QSpinBox, QMenu, QSlider, QAbstractSpinBox, QCheckBox,
)
from .domain import FilterSpec
from .model import FileFilter, PlanModel, ResultModel
from .planning import PlanRequest
from .services import PlanWorker, UndoPlanWorker
from .flow_layout import FlowLayout


class PlanningUI:
    def install_planning(self):
        self._planning_ready = False
        self.plan_worker = None
        self.document_controls = None
        self.last_plan = None
        self.undo_plan = None
        self.last_result_job = None
        self.result_can_undo = False
        self._start_after_plan = False
        self.preview_scope = ""
        self.filter_timer = QTimer(self)
        self.filter_timer.setSingleShot(True)
        self.filter_timer.setInterval(150)
        self.filter_timer.timeout.connect(self.apply_filters)

        self.proxy = FileFilter(self)
        self.proxy.setSourceModel(self.model)
        self.table.setModel(self.proxy)
        self.table.setColumnWidth(3, 136)
        self.table.setColumnWidth(4, 200)
        self.table.setColumnWidth(5, 75)
        self.table.setColumnHidden(4, True)
        self.table.setColumnHidden(5, True)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        self.table.selectionModel().selectionChanged.connect(self._file_selection_changed)

        self.tabs = QTabWidget()
        self.tabs.setAccessibleName("Files, preview and results")
        index = self.file_layout.indexOf(self.table)
        self.file_layout.removeWidget(self.table)
        self.tabs.addTab(self.table, "Files")
        self.preview_table = QTableView()
        self.plan_model = PlanModel(self)
        self.preview_table.setModel(self.plan_model)
        self.preview_table.setAccessibleName("Planned outputs")
        self.preview_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.preview_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.preview_table.setWordWrap(False)
        self.preview_table.setAlternatingRowColors(True)
        self.preview_table.setShowGrid(False)
        self.preview_table.verticalHeader().hide()
        self.preview_table.verticalHeader().setDefaultSectionSize(43)
        self.preview_table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.preview_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.preview_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.preview_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.preview_table.setColumnWidth(2, 100)
        self.preview_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.preview_table.selectionModel().selectionChanged.connect(self._preview_selection_changed)
        self.tabs.addTab(self.preview_table, "Preview")
        self.results_table = QTableView()
        self.result_model = ResultModel(self)
        self.results_table.setModel(self.result_model)
        self.results_table.setAccessibleName("Operation results")
        self.results_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.results_table.setAlternatingRowColors(True)
        self.results_table.setShowGrid(False)
        self.results_table.setWordWrap(False)
        self.results_table.verticalHeader().hide()
        self.results_table.verticalHeader().setDefaultSectionSize(43)
        self.results_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.results_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.results_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.results_table.setColumnWidth(2, 105)
        self.tabs.addTab(self.results_table, "Results")
        self.file_layout.insertWidget(index, self.tabs, 1)

        filters = FlowLayout()
        self.name_filter = QLineEdit()
        self.name_filter.setPlaceholderText("Name contains...")
        self.name_filter.setAccessibleName("Filter by name")
        self.name_filter.setClearButtonEnabled(True)
        self.category_filter = QComboBox()
        self.category_filter.setAccessibleName("Filter by category")
        self.category_filter.addItems(["All types", "Image", "Video", "Audio", "Document", "Archive", "Folder", "Other"])
        filters.addWidget(self.name_filter, 1)
        filters.addWidget(self.category_filter)
        self.file_layout.insertLayout(1, filters)
        self.extension_filter = QLineEdit()
        self.extension_filter.setPlaceholderText("Extensions: jpg, png")
        self.extension_filter.setAccessibleName("Filter by extension")
        self.extension_filter.setClearButtonEnabled(True)
        self.reset_filters_button = QPushButton("Reset filters")
        self.reset_filters_button.clicked.connect(self.reset_filters)
        self.remove_button = QPushButton("Remove selected")
        self.remove_button.setToolTip("Remove highlighted rows from the list. Files on disk are untouched.")
        self.remove_button.clicked.connect(self.remove_selected)
        self.columns_button = QPushButton("Columns")
        menu = QMenu(self.columns_button)
        for column in (3, 4, 5):
            action = menu.addAction(self.model.HEADERS[column])
            action.setCheckable(True)
            action.setChecked(not self.table.isColumnHidden(column))
            action.toggled.connect(lambda checked, col=column: self.table.setColumnHidden(col, not checked))
        self.columns_button.setMenu(menu)
        self.columns_button.setProperty("hasMenu", True)
        filters.insertWidget(1, self.extension_filter, 1)
        self.filter_row = filters
        filters.addWidget(self.reset_filters_button)
        self.file_toolbar.insertWidget(self.file_toolbar.indexOf(self.clear_button), self.remove_button)
        self.file_toolbar.addWidget(self.columns_button)

        self.preview_summary = QLabel("")
        self.preview_summary.setObjectName("muted")
        self.preview_summary.setWordWrap(True)
        self.preview_summary.setTextFormat(Qt.TextFormat.PlainText)
        self.file_layout.addWidget(self.preview_summary)
        self.preview_reason = QLabel("")
        self.preview_reason.setTextFormat(Qt.TextFormat.PlainText)
        self.preview_reason.setWordWrap(True)
        self.preview_reason.setMaximumHeight(54)
        self.preview_reason.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.file_layout.addWidget(self.preview_reason)
        self.undo_bar = QFrame()
        undo_actions = QHBoxLayout(self.undo_bar)
        undo_actions.setContentsMargins(0, 0, 0, 0)
        undo_actions.addStretch()
        self.preview_undo_button = QPushButton("Preview undo")
        self.preview_undo_button.clicked.connect(lambda: self.preview_undo())
        self.apply_undo_button = QPushButton("Apply undo")
        self.apply_undo_button.setObjectName("primary")
        self.apply_undo_button.clicked.connect(self.apply_undo)
        self.discard_undo_button = QPushButton("Back to results")
        self.discard_undo_button.clicked.connect(self.discard_undo)
        for control in (self.preview_undo_button, self.discard_undo_button, self.apply_undo_button):
            undo_actions.addWidget(control)
        self.file_layout.addWidget(self.undo_bar)
        self.file_layout.removeItem(self.file_totals)
        self.file_layout.addLayout(self.file_totals)
        self.compare_button = QPushButton("Compare images")
        self.compare_button.clicked.connect(self.compare_images)
        undo_actions.insertWidget(0, self.compare_button)
        self.stream_details_button = QPushButton("Stream details")
        self.stream_details_button.clicked.connect(self.show_stream_details)
        undo_actions.insertWidget(0, self.stream_details_button)
        self.document_preview_button = QPushButton("Page preview")
        self.document_preview_button.clicked.connect(lambda: self.ensure_document_controls().show_page_preview())
        undo_actions.insertWidget(0,self.document_preview_button)
        self.archive_contents_button = QPushButton("Archive contents")
        self.archive_contents_button.clicked.connect(lambda: self.archive_controls.show_contents())
        self.archive_cleanup_button = QPushButton("Review source cleanup")
        def review_archive_cleanup():
            from .archive_cleanup_ui import review_cleanup
            review_cleanup(self)
        self.archive_cleanup_button.clicked.connect(review_archive_cleanup)
        undo_actions.insertWidget(0, self.archive_cleanup_button)
        undo_actions.insertWidget(0, self.archive_contents_button)
        self.open_video_button = QPushButton("Open output")
        self.open_video_button.clicked.connect(self.open_video_output)
        undo_actions.insertWidget(0, self.open_video_button)
        self.results_table.selectionModel().selectionChanged.connect(self.update_counts)
        self.tabs.currentChanged.connect(self._tab_changed)
        self._tab_changed(0)
        for table in (self.table, self.preview_table, self.results_table):
            table.horizontalHeader().setMinimumSectionSize(96)

        self.destination.setReadOnly(False)
        self.destination.setClearButtonEnabled(True)
        self.destination.setPlaceholderText("Choose an output folder")
        self.destination.setText(str(self.settings.value("output_folder", "")))
        destination_index = self.settings_layout.indexOf(self.destination)
        self.settings_layout.removeWidget(self.destination)
        destination_row = QHBoxLayout()
        destination_row.addWidget(self.destination, 1)
        self.browse_button = QPushButton("...")
        self.browse_button.setToolTip("Choose output folder")
        self.browse_button.setAccessibleName("Choose output folder")
        self.browse_button.clicked.connect(self.choose_destination)
        destination_row.addWidget(self.browse_button)
        self.settings_layout.insertLayout(destination_index, destination_row)

        self.collision_label = QLabel("If output exists")
        self.collision = QComboBox()
        self.collision.addItem("Use a unique name", "unique")
        self.collision.addItem("Skip", "skip")
        self.collision.setAccessibleName("Output conflict policy")
        insert_at = self.settings_layout.indexOf(self.keep)
        self.settings_layout.insertWidget(insert_at, self.collision_label)
        self.settings_layout.insertWidget(insert_at + 1, self.collision)
        self.workflow_policy=QLabel('Workflow: keep originals and use unique output names.')
        self.workflow_policy.setWordWrap(True)
        self.settings_layout.insertWidget(insert_at,self.workflow_policy)
        self.workflow_policy.hide()

        self.prefix_label = QLabel("Prefix")
        self.prefix = QLineEdit("File_")
        self.prefix.setAccessibleName("Rename prefix")
        self.start_label = QLabel("Start at")
        self.start_number = QSpinBox()
        self.start_number.setRange(1, 999999999)
        self.start_number.setValue(1)
        self.start_number.setAccessibleName("First sequence number")
        for control in (self.prefix_label, self.prefix, self.start_label, self.start_number):
            self.settings_layout.insertWidget(self.settings_layout.indexOf(self.keep), control)
        self.pattern_label = QLabel("Only names matching")
        self.rename_pattern = QLineEdit()
        self.rename_pattern.setPlaceholderText("All files if blank; e.g. *.jpg")
        self.rename_pattern.setToolTip("Optional input filter, not the new filename. Only existing names matching this wildcard pattern are renamed; others are skipped. Leave blank for all listed files. Use Prefix to name outputs.")
        self.rename_pattern.setAccessibleName("Rename filename pattern")
        self.separator_label = QLabel("Replace separators with")
        self.separator = QComboBox()
        for title, value in (("Space", " "), ("Underscore", "_"), ("Hyphen", "-")):
            self.separator.addItem(title, value)
        for control in (self.pattern_label, self.rename_pattern, self.separator_label, self.separator):
            self.settings_layout.insertWidget(self.settings_layout.indexOf(self.keep), control)

        self.image_quality_label = QLabel("Quality: 85 / 100")
        self.image_quality = QSlider(Qt.Orientation.Horizontal)
        self.image_quality.setRange(1, 100)
        self.image_quality.setValue(85)
        self.image_quality.setAccessibleName("Image quality")
        self.image_quality.setToolTip("Drag to adjust quality, or use the arrow keys. Range: 1–100.")
        self.image_quality_label.setBuddy(self.image_quality)
        self.image_quality.valueChanged.connect(lambda value: self.image_quality_label.setText(f"Quality: {value} / 100"))
        self.alpha_label = QLabel("JPEG background")
        self.alpha_background = QComboBox()
        for title, color in (("White", "#ffffff"), ("Black", "#000000"), ("Light grey", "#d9d9d9")):
            self.alpha_background.addItem(title, color)
        self.alpha_background.setToolTip("Transparent areas are filled with this color for JPEG.")
        self.metadata_label = QLabel("Metadata")
        self.image_metadata = QComboBox()
        self.image_metadata.addItem("Remove", "remove")
        self.image_metadata.addItem("Preserve supported", "preserve")
        self.image_metadata.setToolTip("Preserve supported EXIF, sRGB ICC and DPI. Comments, XMP and text chunks are omitted.")
        self.image_controls = (self.image_quality_label, self.image_quality, self.alpha_label,
                               self.alpha_background, self.metadata_label, self.image_metadata)
        for control in self.image_controls:
            self.settings_layout.insertWidget(self.settings_layout.indexOf(self.keep), control)
        self.image_quality.valueChanged.connect(self.invalidate_plan)
        self.alpha_background.currentIndexChanged.connect(self.invalidate_plan)
        self.image_metadata.currentIndexChanged.connect(self.invalidate_plan)
        from .resize_ui import ResizeControls
        self.resize_controls = ResizeControls(self)
        from .compression_ui import CompressionControls
        self.compression_controls = CompressionControls(self)
        from .video_format_ui import VideoFormatControls
        self.video_format_controls = VideoFormatControls(self)
        self.video_panel = QFrame()
        video_layout = QVBoxLayout(self.video_panel)
        video_layout.setContentsMargins(0, 0, 0, 0)
        self.video_speed_label = QLabel("Speed")
        video_layout.addWidget(self.video_speed_label)
        self.video_speed = QComboBox()
        for label, preset in (("Balanced", "medium"), ("Fast", "veryfast"), ("Fastest", "superfast")):
            self.video_speed.addItem(label, preset)
        self.video_speed.setCurrentIndex(self.video_speed.findData("veryfast"))
        self.video_speed.setAccessibleName("Video encoding speed")
        self.video_speed.setToolTip("Balanced spends more time compressing; Fastest uses less processing effort. Any speed works with any quality, but file size and retained detail can vary. Fastest is the quickest option offered here.")
        video_layout.addWidget(self.video_speed)
        video_layout.addWidget(QLabel("CPU threads"))
        self.video_threads = QSpinBox()
        self.video_threads.setRange(1, 8)
        self.video_threads.setValue(2)
        video_layout.addWidget(self.video_threads)
        video_layout.addWidget(QLabel("Audio tracks"))
        self.video_audio_mode = QComboBox()
        for label, mode in (("All tracks", "all"), ("Choose tracks", "custom"), ("No audio", "none")):
            self.video_audio_mode.addItem(label, mode)
        video_layout.addWidget(self.video_audio_mode)
        self.video_audio_tracks = QLineEdit()
        self.video_audio_tracks.setPlaceholderText("Audio tracks, e.g. 1, 3")
        self.video_audio_tracks.setAccessibleName("Chosen audio track numbers")
        self.video_audio_tracks.setToolTip("Enter 1 for the first soundtrack, 2 for the second, or 2, 1 for both in that order. See Stream details for track numbers. Missing requested tracks block that file.")
        video_layout.addWidget(self.video_audio_tracks)
        video_layout.addWidget(QLabel("Existing subtitles"))
        self.video_subtitle_mode = QComboBox()
        for label, mode in (("None", "none"), ("All tracks", "all"), ("Choose tracks", "custom")):
            self.video_subtitle_mode.addItem(label, mode)
        self.video_subtitle_mode.setToolTip("Copy compatible subtitles: MP4 timed text; MKV SRT/ASS/SSA/WebVTT; WebM WebVTT. No burning in or subtitle format conversion. Font attachments are omitted and listed for review.")
        video_layout.addWidget(self.video_subtitle_mode)
        self.video_subtitle_tracks = QLineEdit()
        self.video_subtitle_tracks.setPlaceholderText("Subtitle tracks, e.g. 1, 2")
        self.video_subtitle_tracks.setAccessibleName("Chosen subtitle track numbers")
        self.video_subtitle_tracks.setToolTip("Subtitle numbers start at 1 separately in each file; see Stream details. Missing or incompatible selected tracks block that file.")
        video_layout.addWidget(self.video_subtitle_tracks)
        self.video_external_enabled = QCheckBox("Add subtitle file")
        video_layout.addWidget(self.video_external_enabled)
        self.video_external_panel = QFrame()
        external_layout = QVBoxLayout(self.video_external_panel)
        external_layout.setContentsMargins(0, 0, 0, 0)
        self.video_external_subtitle = QLineEdit()
        self.video_external_subtitle.setPlaceholderText("UTF-8 .srt or .vtt file")
        self.video_external_subtitle.setClearButtonEnabled(True)
        self.video_external_subtitle.setAccessibleName("External subtitle file")
        external_layout.addWidget(self.video_external_subtitle)
        self.video_external_browse = QPushButton("Choose subtitle file…")
        def choose_subtitle():
            path, _ = QFileDialog.getOpenFileName(self, "Choose subtitle file", self.video_external_subtitle.text(), "Subtitles (*.srt *.vtt)")
            if path:
                self.video_external_subtitle.setText(path)
        self.video_external_browse.clicked.connect(choose_subtitle)
        external_layout.addWidget(self.video_external_browse)
        hint = QLabel("Adds a selectable subtitle track, default if no kept subtitle is default. The same file is added to each video. Styling may vary by format/player.")
        hint.setWordWrap(True)
        external_layout.addWidget(hint)
        video_layout.addWidget(self.video_external_panel)
        def track_controls():
            self.video_audio_tracks.setVisible(self.video_audio_mode.currentData() == "custom")
            self.video_subtitle_tracks.setVisible(self.video_subtitle_mode.currentData() == "custom")
            self.video_external_panel.setVisible(self.video_external_enabled.isChecked())
            self.invalidate_plan()
        for control in (self.video_audio_mode, self.video_subtitle_mode):
            control.currentIndexChanged.connect(track_controls)
        for control in (self.video_audio_tracks, self.video_subtitle_tracks):
            control.textChanged.connect(self.invalidate_plan)
        self.video_external_enabled.toggled.connect(track_controls)
        self.video_external_subtitle.textChanged.connect(self.invalidate_plan)
        track_controls()
        self.video_omissions = QCheckBox("Allow omitted tracks")
        self.video_omissions.setToolTip("Allow extra video/audio, subtitles, attachments and chapters listed in Stream details to be omitted.")
        video_layout.addWidget(self.video_omissions)
        self.settings_layout.insertWidget(self.settings_layout.indexOf(self.keep), self.video_panel)
        from .audio_ui import AudioControls
        self.audio_controls = AudioControls(self)
        self.original_cleanup_hint = QLabel("Unchecked: originals move to the Recycle Bin after verified success.")
        self.original_cleanup_hint.setWordWrap(True)
        self.settings_layout.insertWidget(self.settings_layout.indexOf(self.keep) + 1, self.original_cleanup_hint)
        self.original_cleanup_hint.hide()
        from .archive_ui import ArchiveControls
        self.archive_controls = ArchiveControls(self)
        self.keep.toggled.connect(lambda: self.original_cleanup_hint.setVisible(self.tool in ("Images", "Video") and not self.keep.isChecked()))
        for control, signal in ((self.video_speed, "currentIndexChanged"), (self.video_threads, "valueChanged"),
                                (self.keep, "toggled"), (self.video_omissions, "toggled")):
            getattr(control, signal).connect(self.invalidate_plan)

        # Keep output settings accessible at smaller window heights.
        for layout in self.settings_panel.findChildren(QVBoxLayout):
            layout.setSpacing(self.settings_layout.spacing())
        scroll = QScrollArea()
        self.settings_scroll = scroll
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFixedWidth(260)
        self.settings_panel.setMinimumWidth(220)
        self.settings_panel.setMaximumWidth(16777215)
        self.columns.replaceWidget(self.settings_panel, scroll)
        scroll.setWidget(self.settings_panel)
        from .controls import SettingsWheelGuard
        self.settings_wheel_guard = SettingsWheelGuard(scroll)
        for control_type in (QComboBox, QAbstractSpinBox, QSlider):
            for control in self.settings_panel.findChildren(control_type):
                control.installEventFilter(self.settings_wheel_guard)

        self.clear_button.setText("Clear list")
        self.preview_button = QPushButton("Preview changes")
        self.preview_button.clicked.connect(self.preview_changes)
        # The status is inside a nested row; use the stored action layout.
        self.actions.insertWidget(self.actions.count() - 1, self.preview_button)
        self.run_button.setObjectName("")
        self.run_button.setText("Run (later)")
        self.run_button.setToolTip("Actual jobs and media operations arrive in later releases.")
        self.run_button.clicked.connect(self.queue_rename)
        self.start_rename_button = QPushButton("Start Rename")
        self.start_rename_button.setObjectName("primary")
        self.start_rename_button.setEnabled(False)
        self.start_rename_button.clicked.connect(self.start_rename)
        # Keep the action sequence stable across tools, with Start at the end.
        for control in (self.cancel_button, self.run_button, self.preview_button):
            self.actions.removeWidget(control)
        for control in (self.issues_button, self.cancel_button, self.run_button,
                        self.preview_button, self.start_rename_button):
            self.actions.addWidget(control)
        # A narrow status column beside five buttons can squeeze the entire file
        # table away at minimum size. Give status its own bounded, full-width row.
        self.actions.removeWidget(self.status)
        self.actions.insertStretch(0, 1)
        main_layout = self.main_workspace.layout()
        main_layout.insertWidget(main_layout.indexOf(self.progress), self.status)
        self.format.currentIndexChanged.connect(self._rename_controls)
        self.rename_pattern.textChanged.connect(self.invalidate_plan)
        self.separator.currentIndexChanged.connect(self.invalidate_plan)

        for control in (self.name_filter, self.extension_filter):
            control.textChanged.connect(lambda: self.filter_timer.start())
        self.category_filter.currentIndexChanged.connect(self.apply_filters)
        for control, signal in ((self.destination, "textChanged"), (self.prefix, "textChanged"),
                                (self.start_number, "valueChanged"), (self.collision, "currentIndexChanged"),
                                (self.format, "currentIndexChanged"), (self.detail, "currentIndexChanged")):
            getattr(control, signal).connect(self.invalidate_plan)
        self.destination.textChanged.connect(self._destination_hint)
        self.destination.editingFinished.connect(self.save_destination)
        self.model.rowsInserted.connect(self.update_counts)
        self.model.modelReset.connect(self.update_counts)
        self.workflow_bar=QFrame()
        workflow_layout=QHBoxLayout(self.workflow_bar)
        workflow_layout.setContentsMargins(0,0,0,0)
        self.workflow_label=QLabel()
        self.workflow_label.setWordWrap(True)
        self.workflow_capture_button=QPushButton('Add step to workflow')
        self.workflow_capture_button.setToolTip('Save these settings as a workflow step. Workflow originals are always kept.')
        self.workflow_capture_button.clicked.connect(lambda:self.ensure_jobs().workflow_editor.capture())
        back=QPushButton('Review workflow')
        back.clicked.connect(self.review_workflow)
        workflow_layout.addWidget(self.workflow_label,1)
        workflow_layout.addWidget(self.workflow_capture_button)
        workflow_layout.addWidget(back)
        leave=QPushButton('Exit planning')
        leave.clicked.connect(lambda:self.ensure_jobs().workflow_editor.leave())
        workflow_layout.addWidget(leave)
        self.main_workspace.layout().insertWidget(1,self.workflow_bar)
        self.workflow_bar.hide()
        self._planning_ready = True
        self.update_counts()

    def ensure_document_controls(self):
        if self.document_controls is None:
            from .document_ui import DocumentControls
            self.document_controls = DocumentControls(self)
            for kind in (QComboBox,QAbstractSpinBox):
                for control in self.document_controls.panel.findChildren(kind):
                    control.installEventFilter(self.settings_wheel_guard)
        return self.document_controls

    def _tool_changed(self, title):
        visual=getattr(self,'visual',None)
        if visual:visual.before_tool_change()
        if title=='Queue':
            self.open_jobs()
            return
        if title == "Folders":
            self.show_folders()
            return
        if title == "Documents":
            self.ensure_document_controls()
        self.workspace_stack.setCurrentWidget(self.main_workspace)
        self._planning_ready = False
        previous = self.tool
        if title == "Rename" and previous != "Rename":
            self._conversion_destination = self.destination.text()
        self._base_tool_changed(title)
        if hasattr(self,'input_types'):self.input_types.configure(title)
        if title == "Rename":
            self.destination.clear()
        elif previous == "Rename":
            self.destination.setText(getattr(self, "_conversion_destination", ""))
        for control in (self.prefix_label, self.prefix, self.start_label, self.start_number):
            control.setVisible(title == "Rename")
        self.destination.setEnabled(title != "Rename")
        self.browse_button.setEnabled(title != "Rename")
        for control in (self.destination_label, self.destination, self.browse_button, self.keep):
            control.setVisible(title != "Rename")
        self.keep.setVisible(title in ("Images", "Video"))
        self.recursive.setVisible(title != "Archives")
        self.add_button.setToolTip('Choose files and folders together. Drop them onto Files to add them directly.')
        self.open_folder_action.setToolTip(
            "Add a folder with its subfolders intact."
            if title == "Archives" else "List files from a folder; Subfolders controls recursion.")
        self.phase_hint.hide()
        self.destination.setPlaceholderText("Same folder" if title == "Rename" else "Input folder")
        self._destination_hint()
        self.phase_hint.setText("Preview names and conflicts. Processing arrives in a later release.")
        self._planning_ready = True
        self._rename_controls()
        self.apply_filters(force=True)
        self.resize_controls.set_tool(title)
        self.compression_controls.update_visibility()
        self.video_format_controls.update_visibility()
        self.video_panel.setVisible(title == "Video")
        self.audio_controls.update_visibility()
        if self.document_controls is not None:
            self.document_controls.update_visibility()
        self.archive_controls.update_visibility()
        self.original_cleanup_hint.setVisible(title in ("Images", "Video") and not self.keep.isChecked())
        self.preview_table.setColumnHidden(3, title == "Rename")
        self.plan_model.contents_mode = title == "Archives"
        self.plan_model.audio_mode = title == "Audio"
        self.plan_model.document_mode = title == "Documents"
        self.plan_model.headerDataChanged.emit(Qt.Orientation.Horizontal, 3, 3)
        self.invalidate_plan()
        self.update_controls()
        self.status.clear()
        if self.jobs_dialog is not None:self.jobs_dialog.workflow_editor.update_banner()
        if visual:visual.tool_changed()

    def review_workflow(self):
        self.open_jobs()
        self.jobs_dialog.tabs.setCurrentWidget(self.jobs_dialog.workflow_editor)

    def editing_workflow(self):
        return self.jobs_dialog is not None and self.jobs_dialog.workflow_editor.active

    def _rename_controls(self, *args):
        if not self._planning_ready:
            return
        rename = self.tool == "Rename"
        style = self.format.currentIndex() if rename else -1
        for control in (self.prefix_label, self.prefix):
            control.setVisible(rename and style in (0, 1))
        for control in (self.start_label, self.start_number):
            control.setVisible(rename and style == 0)
        self.detail.setVisible(not rename or style == 0)
        self.detail_title.setVisible(not rename or style == 0)
        if self.tool in ("Images", "Audio", "Documents"):
            self.detail.hide()
            self.detail_title.hide()
        for control in self.image_controls:
            control.setVisible(self.tool == "Images")
        for control in (self.alpha_label, self.alpha_background):
            control.setVisible(self.tool == "Images" and self.format.currentText() == "JPEG")
        for control in (self.image_quality_label, self.image_quality):
            control.setVisible(self.tool == "Images" and self.format.currentText() != "PNG")
        for control in (self.pattern_label, self.rename_pattern):
            control.setVisible(rename)
        for control in (self.separator_label, self.separator):
            control.setVisible(rename and style == 2)
        self.compression_controls.update_visibility()
        self.archive_controls.update_visibility()

    def queue_rename(self):
        if self.editing_workflow():return self.jobs_dialog.workflow_editor.capture()
        if self.tool in ("Images", "Video", "Audio", "Documents", "Archives"):
            return self.convert_images(queued=True)
        try:
            if self.editing_busy or self.tool != "Rename" or self.last_plan is None:
                return
            from .renaming import rename_snapshot
            specs = rename_snapshot(self.last_plan)
            self.open_jobs()
            queue = self.jobs_dialog
            if queue is None:
                return
            job_id = queue.store.enqueue("Rename files", "rename", specs)
            queue.refresh(job_id)
            self.invalidate_plan()
            self.status.setText("Rename batch queued. Choose Start queue in Queue.")
            if queue.enabled:
                queue._next()
        except Exception as error:
            self.status.setText(str(error))

    def start_rename(self):
        if self.editing_workflow():return self.jobs_dialog.workflow_editor.capture()
        if self.tool in ("Images", "Video", "Audio", "Documents", "Archives"):
            return self.convert_images()
        try:
            if self.editing_busy or self.tool != "Rename":
                return
            self.apply_filters()
            queue = self.ensure_jobs()
            if not queue.can_start_immediately:
                self.status.setText("Another task is active. Wait for it to finish, or choose Queue rename.")
                return
            if self.last_plan is None:
                if self.preview_changes():
                    self._start_after_plan = True
                return
            from .renaming import rename_snapshot
            specs = rename_snapshot(self.last_plan)
            job_id = queue.store.enqueue("Rename files", "rename", specs)
            self.invalidate_plan()
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            self.status.setText("Starting rename…")
            queue.start_single(job_id)
        except Exception as error:
            self.status.setText(str(error))

    def apply_filters(self, *args, force=False):
        if not self._planning_ready or (self.editing_busy and not force):
            return
        self.filter_timer.stop()
        spec = self.input_types.spec() if hasattr(self,'input_types') else FilterSpec.parse(
            self.category_filter.currentText(),self.name_filter.text(),self.extension_filter.text())
        if spec != self.proxy.spec:
            self.table.clearSelection()
            self.proxy.set_spec(spec)
            self.invalidate_plan()
        self.update_counts()

    def reset_filters(self):
        if self.editing_busy:
            return
        self.name_filter.clear()
        self.extension_filter.clear()
        self.category_filter.setCurrentIndex(0)
        self.apply_filters()

    def _file_selection_changed(self, *args):
        if self._planning_ready and not self.plan_worker:
            self.invalidate_plan()
        self.update_counts()

    def update_counts(self, *args):
        if not getattr(self, "_planning_ready", False):
            return
        visible = self.proxy.rowCount()
        total = self.input_types.total() if hasattr(self,'input_types') else self.model.rowCount()
        selected = len(self.table.selectionModel().selectedRows())
        self.count_label.setText(f"{visible:,} visible / {total:,} total / {selected:,} selected")
        if hasattr(self,'input_types'):self.input_types.refresh()
        self.issues_button.setText(f"Scan details ({len(self.issues)})" if self.issues else "Scan details")
        self.preview_button.setEnabled(not self.editing_busy and visible > 0)
        rename = self.tool in ("Rename", "Images", "Video", "Audio", "Documents", "Archives")
        self.run_button.setText("Add to Queue" if rename else "Run (later)")
        valid = self.last_plan is not None and any(item.status == "Planned" for item in self.last_plan.items)
        invalid = self.last_plan is not None and any(item.status in ("Invalid", "Sample", "Incompatible", "Review") for item in self.last_plan.items)
        self.run_button.setEnabled(rename and not self.editing_busy and valid and not invalid)
        self.start_rename_button.setVisible(rename)
        self.start_rename_button.setText("Start" if self.tool=="Documents" else "Start Audio" if self.tool == "Audio" else "Create " + self.format.currentText() if self.tool == "Archives" else "Start Remux" if self.tool == "Video" and self.video_format_controls.action.currentData() == "remux" else
            "Start Compress" if self.tool == "Video" and self.compression_controls.mode.currentData() == "target_size"
            else ("Start Resize" if self.resize_controls.mode.currentData() != "none" else "Start Convert")
            if self.tool in ("Images", "Video") else "Start Rename")
        can_start = self.jobs_dialog is None or self.jobs_dialog.can_start_immediately
        self.start_rename_button.setEnabled(rename and not self.editing_busy and visible > 0 and can_start and self.undo_plan is None)
        self.start_rename_button.setToolTip("Validate and process the selected or visible files. Preview is optional." if can_start else
                                          "Another task is active. Wait for it, or add to Queue.")
        self.run_button.setToolTip("Queue the exact settings and outputs shown in this preview." if rename else "Media processing follows in later phases.")
        self.remove_button.setEnabled(not self.editing_busy and selected > 0 and self.tabs.currentIndex() == 0)
        editing=self.editing_workflow()
        self.preview_button.setText('Review workflow' if editing else 'Preview changes')
        if editing:
            self.preview_button.setEnabled(not self.editing_busy)
            self.start_rename_button.setText('Add step')
            self.start_rename_button.setEnabled(not self.editing_busy)
            self.start_rename_button.setToolTip('Save the current tool settings into the workflow. Files are processed when the workflow starts.')
        self.run_button.setVisible(not editing)
        self._update_undo_controls()
        if hasattr(self,'visual'):self.visual.refresh()

    def _update_undo_controls(self):
        if not hasattr(self, "undo_bar"):
            return
        previewing = self.undo_plan is not None and self.tabs.currentIndex() == 1
        results = self.tabs.currentIndex() == 2 and self.last_result_job is not None
        video_preview = self.tabs.currentIndex() == 1 and self.last_plan is not None and self.last_plan.request.operation == "video"
        archive_preview = self.tabs.currentIndex() == 1 and self.last_plan is not None and self.last_plan.request.operation == "archives"
        document_preview = self.tabs.currentIndex()==1 and self.last_plan is not None and self.last_plan.request.operation=="documents"
        self.undo_bar.setVisible(previewing or results or video_preview or archive_preview or document_preview)
        self.document_preview_button.setVisible(document_preview and any(i.document_spec and Path(i.source.path).suffix.lower()==".pdf" for i in self.last_plan.items))
        self.document_preview_button.setEnabled(not self.editing_busy)
        self.archive_contents_button.setVisible(archive_preview)
        self.archive_contents_button.setEnabled(not self.editing_busy)
        self.archive_cleanup_button.setVisible(results and getattr(self, "result_is_archives", False))
        self.archive_cleanup_button.setEnabled(not self.editing_busy and self.jobs_dialog is not None and self.jobs_dialog.can_start_immediately)
        self.stream_details_button.setVisible(video_preview)
        self.stream_details_button.setEnabled(not self.editing_busy)
        self.open_video_button.setVisible(results and (getattr(self,"result_is_documents",False) or getattr(self, "result_is_audio", False) or getattr(self, "result_is_video", False) or getattr(self, "result_is_archives", False)))
        rows=self.results_table.selectionModel().selectedRows()
        candidates=[self.result_model.items[r.row()] for r in rows if r.row()<len(self.result_model.items)] if rows else self.result_model.items
        self.open_video_button.setEnabled(any(i['state']=='succeeded' and i.get('output') for i in candidates))
        self.preview_undo_button.setVisible(results and self.result_can_undo)
        self.discard_undo_button.setVisible(previewing)
        self.apply_undo_button.setVisible(previewing)
        free = not self.editing_busy and (self.jobs_dialog is None or self.jobs_dialog.can_start_immediately)
        self.preview_undo_button.setEnabled(free and self.result_can_undo)
        self.discard_undo_button.setEnabled(not self.editing_busy)
        self.apply_undo_button.setEnabled(free and previewing and self.undo_plan.can_apply)
        self.compare_button.setVisible(results and getattr(self, "result_is_images", False))
        self.compare_button.setEnabled(not self.editing_busy and bool(self.results_table.selectionModel().selectedRows()))

    def update_controls(self):
        working = self.editing_busy
        # Folder scans/filters do not need to restyle every hidden media control
        # or restart image-preview bookkeeping. Refresh the active page only.
        if self.folders_active:
            self.folders_page.update_controls()
            for control in self.nav_group.buttons():
                control.setEnabled(not working or control is self.jobs_button)
            return
        if self.folders_page is not None:
            self.folders_page.update_controls()
        self.resize_controls.set_enabled(not working)
        self.workflow_capture_button.setEnabled(not working)
        self.video_panel.setEnabled(not working)
        self.audio_controls.panel.setEnabled(not working)
        if self.document_controls is not None:
            self.document_controls.panel.setEnabled(not working)
        self.compression_controls.panel.setEnabled(not working)
        self.video_format_controls.panel.setEnabled(not working)
        self.archive_controls.panel.setEnabled(not working)
        self.archive_controls.name.setEnabled(not working)
        self.keep.setEnabled(not working and self.tool in ("Images", "Video"))
        editing=self.editing_workflow()
        self.keep.setVisible(not editing and self.tool in ('Images','Video'))
        self.original_cleanup_hint.setVisible(not editing and self.tool in ('Images','Video') and not self.keep.isChecked())
        self.workflow_policy.setVisible(editing)
        self.collision_label.setVisible(not editing)
        self.collision.setVisible(not editing)
        for control in (self.add_button, self.open_files_action, self.open_folder_action,
                        self.clear_button, self.recursive, self.name_filter,
                        self.extension_filter, self.category_filter, self.reset_filters_button,
                        self.collision, self.prefix, self.start_number, self.format, self.detail):
            control.setEnabled(not working)
        self.rename_pattern.setEnabled(not working)
        self.separator.setEnabled(not working)
        for control in self.image_controls:
            control.setEnabled(not working)
        self.destination.setEnabled(not working and self.tool != "Rename")
        self.browse_button.setEnabled(not working and self.tool != "Rename")
        for control in self.nav_group.buttons():
            control.setEnabled(not working or control is self.jobs_button)
        self.table.setEnabled(self.plan_worker is None)
        self.cancel_button.setEnabled(working)
        self.cancel_button.setText("Cancel" if self.plan_worker else "Cancel scan")
        if not working and self.jobs_dialog is not None and self.jobs_dialog.worker is not None and self.jobs_dialog.worker.adapter == "rename":
            undoing = self.jobs_dialog.store.original_job(self.jobs_dialog.worker.job_id)
            self.cancel_button.setText("Cancel undo" if undoing else "Cancel rename")
        if not working and self.jobs_dialog is not None and self.jobs_dialog.worker is not None and self.jobs_dialog.worker.adapter in ("images", "video", "audio"):
            self.cancel_button.setText("Cancel conversion")
        if not working and self.jobs_dialog is not None and self.jobs_dialog.worker is not None and self.jobs_dialog.worker.adapter == "archives":
            self.cancel_button.setText("Cancel archive")
        self.update_counts()

    def invalidate_plan(self, *args):
        if not getattr(self, "_planning_ready", False):
            return
        self.last_plan = None
        self.undo_plan = None
        self.run_button.setEnabled(False)
        self.start_rename_button.setEnabled(False)
        self.plan_model.set_items()
        self.preview_reason.clear()
        self.preview_summary.clear()
        self._tab_changed(self.tabs.currentIndex())
        self.resize_controls.schedule()
        self.update_counts()

    def remove_selected(self):
        if self.editing_busy or self.tabs.currentIndex() != 0:
            return
        rows = [self.proxy.mapToSource(index).row() for index in self.table.selectionModel().selectedRows()]
        if rows:
            self.invalidate_plan()
            self.model.remove_indices(rows)
            self.status.setText(f"Removed {len(rows):,} entries from the list.")
            self.update_counts()

    def save_destination(self):
        if self.tool != "Rename":
            self.settings.setValue("output_folder", self.destination.text().strip())

    def _destination_hint(self, *args):
        if self.tool == "Archives":
            self.destination.setToolTip(self.destination.text().strip() or "Blank: combined archives save beside the first input in sorted order; separate archives save beside each input. Type, paste or browse to an existing folder to override.")
            return
        self.destination.setToolTip(self.destination.text().strip() or (
            "Save beside each input file. Choose another folder to override; clear this field to use input folders again."
            if self.tool in ("Images", "Video", "Audio", "Documents") else "Choose an output folder."))

    def choose_destination(self):
        path = QFileDialog.getExistingDirectory(self, "Choose output folder", self.destination.text())
        if path:
            self.destination.setText(path)
            self.settings.setValue("output_folder", path)

    def plan_request(self):
        operation = {"Images": "images", "Video": "video", "Audio": "audio", "Documents":"documents", "Rename": "rename", "Archives": "archives"}[self.tool]
        remux = operation == "video" and self.video_format_controls.action.currentData() == "remux"
        resize_options = self.resize_controls.options()
        compression_options = self.compression_controls.options()
        if remux:
            resize_options["resize_mode"] = "none"
            compression_options["video_mode"] = "quality"
        extension = {"JPEG": "jpg", "PNG": "png", "WebP": "webp",
                     "MP4": "mp4", "MKV": "mkv", "WebM": "webm", "MP3": "mp3", "M4A / AAC": "m4a",
                     "FLAC": "flac", "WAV": "wav", "Opus": "opus", "PDF":"pdf", "Searchable PDF":"pdf", "TXT":"txt"}.get(self.format.currentText(), "")
        return PlanRequest(operation, self.destination.text().strip(), extension,
                           self.collision.currentData(), self.prefix.text(), self.start_number.value(),
                           int(self.detail.currentData() or 4), self.detail.currentText(),
                           rename_style=("sequence", "random", "clean")[self.format.currentIndex()] if operation == "rename" else "sequence",
                           separator=self.separator.currentData(), pattern=self.rename_pattern.text(),
                           seed=uuid.uuid4().hex if operation == "rename" and self.format.currentIndex() == 1 else "",
                           image_quality=self.image_quality.value(), alpha_background=self.alpha_background.currentData(),
                           metadata=self.image_metadata.currentData(), **resize_options,
                           video_crf=int(self.detail.currentData() or 23) if operation == "video" else 23,
                           video_preset=self.video_speed.currentData(), video_threads=self.video_threads.value(),
                           video_audio=1, video_allow_omissions=self.video_omissions.isChecked(),
                           keep_originals=True if operation in ("archives", "audio", "documents") else self.keep.isChecked(),
                           video_audio_mode=self.video_audio_mode.currentData(), video_audio_tracks=self.video_audio_tracks.text(),
                           video_subtitle_mode=self.video_subtitle_mode.currentData(), video_subtitle_tracks=self.video_subtitle_tracks.text(),
                           video_external_enabled=self.video_external_enabled.isChecked(), video_external_subtitle=self.video_external_subtitle.text(),
                           video_codec=self.video_format_controls.codec.currentData(),
                           video_action="remux" if remux else "convert", **compression_options,
                           **self.archive_controls.options(), **self.audio_controls.options(),
                           **(self.ensure_document_controls().options() if operation=="documents" else {}))

    def preview_changes(self):
        if self.editing_workflow():
            self.review_workflow()
            return False
        if self.editing_busy:
            return False
        if self.tool == "Archives" and getattr(self, "archive_intake_incomplete", False):
            self.status.setText("ZIP intake was incomplete. Check Scan details, then Clear list and add the complete selection again.")
            return False
        self.apply_filters()
        selected = self.table.selectionModel().selectedRows()
        if selected:
            records = tuple(self.model.items[self.proxy.mapToSource(index).row()] for index in selected)
            self.preview_scope = f"{len(records):,} highlighted {'inputs' if self.tool == 'Archives' else 'files'}"
        else:
            records = tuple(record for record in self.model.items if self.proxy.spec.matches(record))
            self.preview_scope = f"All {len(records):,} visible {'inputs' if self.tool == 'Archives' else 'files'}"
        if not records:
            self.status.setText("Add files or reset filters before previewing.")
            return False
        self.invalidate_plan()
        try:
            request = self.plan_request()
        except Exception as error:
            self.status.setText(str(error))
            return False
        self.plan_worker = PlanWorker(records, request, self)
        if hasattr(self,'waveform'):self.waveform.cancel()
        self.plan_worker.planned.connect(self._plan_received)
        self.plan_worker.failed.connect(lambda message: self.status.setText(message))
        self.plan_worker.finished.connect(self._plan_finished)
        self.update_controls()
        self.progress.setRange(0, 0)
        self.status.setText("Checking source snapshots and output names...")
        self.plan_worker.start()
        return True

    def _plan_received(self, plan):
        if self._closing:
            return
        self.last_plan = plan
        self.plan_model.set_items(plan.items)
        counts = " / ".join(f"{name}: {count:,}" for name, count in sorted(plan.counts.items()))
        notices = f" Scan notices: {len(self.issues)}." if self.issues else ""
        self.preview_summary.setText(f"{self.preview_scope}. {counts}.{notices}")
        self.preview_reason.setText("\n".join(plan.warnings))
        if not any(i.status=='Planned' for i in plan.items):
            self.preview_reason.setText('\n'.join(dict.fromkeys(i.reason for i in plan.items if i.reason)))
        self.preview_reason.setToolTip("\n".join(plan.warnings))
        self.tabs.setCurrentIndex(1)
        self._tab_changed(1)
        self.status.setText("")

    def _plan_finished(self):
        worker = self.plan_worker
        cancelled = worker.isInterruptionRequested()
        self.plan_worker = None
        worker.deleteLater()
        if cancelled:
            self.invalidate_plan()
            self.status.setText("Preview cancelled.")
        self.progress.setRange(0, 100)
        self.progress.setValue(0 if cancelled else 100)
        self.update_controls()
        self.plan_complete.emit()
        auto_start = self._start_after_plan
        self._start_after_plan = False
        if auto_start and not cancelled and not self._closing and self.last_plan is not None:
            # Run the exact validated snapshot, including the random-name seed.
            self.start_rename()
        if self._closing:
            self.close()

    def _tab_changed(self, index):
        self.preview_summary.setVisible(index == 1 and bool(self.preview_summary.text()))
        self.preview_reason.setVisible(index == 1 and bool(self.preview_reason.text()))
        if getattr(self, "_planning_ready", False):
            self.update_counts()
        if hasattr(self,'visual'):self.visual.tab_changed(index)

    def _preview_selection_changed(self, *args):
        rows = self.preview_table.selectionModel().selectedRows()
        if rows:
            item = self.plan_model.items[rows[0].row()]
            text = f"{item.reason}\n{item.source.path} -> {item.output or '-'}"
            self.preview_reason.setText(item.reason)
            self.preview_reason.setToolTip(text)
            self._tab_changed(self.tabs.currentIndex())

    def show_rename_results(self, items, job_id=None, is_undo=False, images=False, video=False, archives=False, audio=False, documents=False,focus=True):
        if hasattr(self,'visual'):
            adapter='documents' if documents else 'audio' if audio else 'images' if images else 'video' if video else 'archives' if archives else 'rename'
            from .progress_ui import TOOLS
            self.visual.activity.remember(TOOLS[adapter],job_id,adapter)
            if self.tool!=TOOLS[adapter]:return
        self.result_is_documents = documents
        self.result_is_audio = audio
        self.result_is_images = images
        self.result_is_video = video
        self.result_is_archives = archives
        results = []
        finished = set()
        for item in items:
            spec = json.loads(item["spec"])
            cleanup_details = ""
            if archives and item.get("original_cleanup"):
                cleanup = json.loads(item["original_cleanup"])
                cleanup_details = f" Source cleanup: {cleanup.get('moved', 0)} moved; {cleanup.get('kept', 0)} kept."
                reasons = {v["message"] for v in cleanup.get("files", {}).values() if v["state"] != "moved"}
                cleanup_details += " " + " ".join(sorted(reasons)) if reasons else ""
            results.append(dict(source=spec["source"], output=spec.get("output") or "",
                                state=item["state"], details=((item.get("message", "") or "").split(" Source cleanup:", 1)[0]
                                    .replace(" Originals kept.", "") if cleanup_details else (item.get("message", "") or "")) + cleanup_details,
                                source_label=spec.get("name") if archives or audio or documents else None))
            if item["state"] in ("succeeded", "skipped"):
                if documents:
                    for source in spec.get("inputs",[]):
                        finished.add((source["source"],*source["identity"][:2]))
                if audio:
                    for source in spec.get("sources", []):
                        finished.add((source["source"], *source["identity"][:2]))
                if archives:
                    for root in spec.get("inputs", []):
                        finished.add((root["source"], *root["identity"][:2]))
                for path in (spec["source"], spec.get("output")):
                    if path:
                        finished.add((path, *spec["identity"][:2]))
        if audio:
            incomplete_sources = {source["source"] for item in items if item["state"] not in ("succeeded", "skipped")
                                  for source in json.loads(item["spec"]).get("sources", [json.loads(item["spec"])])}
            finished = {key for key in finished if key[0] not in incomplete_sources}
        if documents:
            incomplete_sources = {s["source"] for item in items if item["state"] not in ("succeeded","skipped")
                                  for s in json.loads(item["spec"]).get("inputs",[json.loads(item["spec"])])}
            finished = {key for key in finished if key[0] not in incomplete_sources}
        self.invalidate_plan()
        self.model.replace_order([record for record in self.model.items
                                  if (record.path, record.device, record.inode) not in finished])
        self.result_model.set_items(tuple(results))
        self.last_result_job = job_id
        self.result_can_undo = (bool(job_id) and not is_undo and not images and not video and not archives and not audio and not documents
                               and any(item["state"] == "succeeded" for item in items)
                               and all(item["state"] in ("succeeded", "skipped") for item in items)
                               and not self.jobs_dialog.store.undo_job(job_id))
        if focus:self.tabs.setCurrentIndex(2)
        self.update_counts()

    def convert_images(self, queued=False):
        if self.editing_workflow():return self.jobs_dialog.workflow_editor.capture()
        try:
            if self.editing_busy or self.tool not in ("Images", "Video", "Audio", "Documents", "Archives"):
                return
            self.apply_filters()
            queue = self.ensure_jobs()
            if not queued and not queue.can_start_immediately:
                raise ValueError("Another task is active. Wait for it, or add to Queue.")
            if self.last_plan is None:
                if not queued and self.preview_changes():
                    self._start_after_plan = True
                return
            if self.tool == "Archives":
                from .zip_plan import archive_snapshot
                specs = self.archive_controls.authorize(archive_snapshot(self.last_plan))
                title, adapter = "Create " + self.last_plan.request.archive_format.upper() + " archives", "archives"
            elif self.tool == "Documents":
                from .documents import document_snapshot
                specs = document_snapshot(self.last_plan)
                title,adapter = self.document_controls.task.currentText(),"documents"
            elif self.tool == "Audio":
                from .audio import audio_snapshot
                specs = audio_snapshot(self.last_plan)
                task = self.last_plan.request.audio_action
                title = {"trim": "Trim audio", "split": "Split audio", "join": "Join audio"}.get(task,
                    "Copy audio tracks" if self.last_plan.request.audio_mode == "copy" else "Convert / extract audio")
                adapter = "audio"
            elif self.tool == "Video":
                from .video import video_snapshot
                specs = video_snapshot(self.last_plan)
                title = ("Remux videos" if self.last_plan.request.video_action == "remux" else
                         "Compress videos to target size" if self.last_plan.request.video_mode == "target_size" else
                         "Resize and convert videos" if self.last_plan.request.resize_mode != "none" else "Convert videos")
                adapter = "video"
            else:
                from .images import image_snapshot
                specs = image_snapshot(self.last_plan)
                title = "Resize and convert images" if self.last_plan.request.resize_mode != "none" else "Convert images"
                adapter = "images"
            job_id = queue.store.enqueue(title, adapter, specs)
            if adapter == "archives":
                self.archive_controls.password.clear()
                self.archive_controls.confirm_password.clear()
            self.invalidate_plan()
            if queued:
                queue.refresh(job_id)
                queue.show()
                self.status.setText("Archive batch queued. Choose Start queue in Queue." if adapter == "archives" else "Remux queued. Choose Start queue in Queue." if title == "Remux videos" else "Conversion queued. Choose Start queue in Queue.")
                if queue.enabled:
                    queue._next()
            else:
                action = "Processing documents" if adapter=="documents" else "Copying audio tracks" if title == "Copy audio tracks" else "Processing audio tracks" if adapter == "audio" else "Archiving files" if adapter == "archives" else "Remuxing files" if title == "Remux videos" else "Converting files"
                self.status.setText(action + (". Originals are kept…" if adapter in ("archives", "audio", "documents") or self.keep.isChecked() else ". Originals move to Recycle Bin after verification…"))
                self.progress.setRange(0, 100)
                self.progress.setValue(0)
                queue.start_single(job_id)
        except Exception as error:
            self.status.setText(str(error))

    def compare_images(self):
        rows = self.results_table.selectionModel().selectedRows()
        if self.editing_busy or not rows or not getattr(self, "result_is_images", False):
            return
        item = self.result_model.items[rows[0].row()]
        if item["state"] != "succeeded":
            self.status.setText("Select a successfully converted image to compare.")
            return
        from .image_compare import CompareDialog
        dialog = CompareDialog(item, self)
        self.comparison_dialog = dialog
        dialog.destroyed.connect(lambda: setattr(self, "comparison_dialog", None))
        dialog.show()

    def show_stream_details(self):
        if self.editing_busy or self.last_plan is None or self.last_plan.request.operation != "video":
            return
        rows = self.preview_table.selectionModel().selectedRows()
        selected = [self.last_plan.items[rows[0].row()]] if rows else self.last_plan.items
        from .video import describe
        sections = []
        for item in selected:
            lines = [item.source.name, item.reason]
            if item.video_spec:
                for kind in ("audio", "subtitle"):
                    streams = [s for s in item.video_spec["video"]["streams"] if s["codec_type"] == kind]
                    for ordinal, stream in enumerate(streams, 1):
                        flags = ", ".join(flag for flag in ("default", "forced") if stream.get(flag))
                        lines.append(f"{kind.title()} track {ordinal}: {describe(stream)}" + (f" ({flags})" if flags else ""))
            sections.append("\n".join(lines))
        self._show_text("Video stream selection", "\n\n".join(sections))

    def open_video_output(self):
        rows = self.results_table.selectionModel().selectedRows()
        candidates=[self.result_model.items[r.row()] for r in rows if r.row()<len(self.result_model.items)] if rows else self.result_model.items
        item=next((i for i in candidates if i['state']=='succeeded' and i.get('output')),None)
        from .output_paths import resolved_output,open_output_path
        store=self.jobs_dialog.store if self.jobs_dialog else None
        path=resolved_output(store,self.last_result_job,item['output']) if item else None
        return open_output_path(path,self.status.setText)

    def preview_undo(self, job_id=None):
        if self.editing_busy:
            return False
        job_id = job_id or self.last_result_job
        if not job_id:
            return False
        try:
            queue = self.ensure_jobs()
            if not queue.can_start_immediately:
                raise ValueError("Wait for the current task to finish or pause the queue before previewing undo.")
            if self.tool != "Rename":
                self._tool_changed("Rename")
            self.invalidate_plan()
            self._start_after_plan = False
            self.plan_worker = UndoPlanWorker(queue.directory, job_id, self)
            self.plan_worker.planned.connect(self._undo_received)
            self.plan_worker.failed.connect(lambda message: self.status.setText(message))
            self.plan_worker.finished.connect(self._undo_plan_finished)
            self.update_controls()
            self.progress.setRange(0, 0)
            self.status.setText("Checking renamed files and original names for undo…")
            self.plan_worker.start()
            return True
        except Exception as error:
            self.status.setText(str(error))
            return False

    def _undo_received(self, plan):
        if self._closing:
            return
        self.undo_plan = plan
        self.plan_model.set_items(plan.items)
        conflicts = sum(item.status == "Conflict" for item in plan.items)
        self.preview_summary.setText(f"Undo preview: {len(plan.items)} files; {conflicts} conflicts. Output names are the original names.")
        self.preview_reason.setText("Resolve every conflict, then preview again." if conflicts else
                                    "Apply undo restores these exact original names.")
        self.tabs.setCurrentIndex(1)
        self.status.setText("Undo blocked by conflicts. No files changed." if conflicts else
                            "Undo preview ready. Review the names, then Apply undo.")

    def _undo_plan_finished(self):
        worker, self.plan_worker = self.plan_worker, None
        cancelled = worker.isInterruptionRequested()
        worker.deleteLater()
        if cancelled:
            self.invalidate_plan()
            self.status.setText("Undo preview cancelled. No files changed.")
        self.progress.setRange(0, 100)
        self.progress.setValue(0 if cancelled else 100)
        self.update_controls()
        self.plan_complete.emit()
        if self._closing:
            self.close()

    def apply_undo(self):
        plan = self.undo_plan
        if self.editing_busy or plan is None or not plan.can_apply:
            return
        try:
            queue = self.ensure_jobs()
            if not queue.can_start_immediately:
                raise ValueError("Wait for the current task to finish before applying undo.")
            job_id = queue.store.enqueue_undo(plan.original_job, plan.specs)
            self.result_can_undo = False
            self.invalidate_plan()
            self.status.setText("Restoring original names…")
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            queue.start_single(job_id)
        except Exception as error:
            self.status.setText(str(error))

    def discard_undo(self):
        if not self.editing_busy:
            self.invalidate_plan()
            self.tabs.setCurrentIndex(2)
            self.status.setText("Undo preview closed. No files changed.")
