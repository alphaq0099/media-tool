"""Resize settings and a single background dimension-preview worker."""
from PySide6.QtCore import QObject, QThread, QTimer, Signal, Qt, QSignalBlocker
from PySide6.QtWidgets import QFrame, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QSpinBox, QCheckBox, QLayout
from .resizing import resize_dimensions


class DimensionWorker(QThread):
    ready = Signal(object, object, str)

    def __init__(self, record, key, parent):
        super().__init__(parent)
        self.record, self.key = record, key

    def run(self):
        size, error = None, ""
        try:
            from .jobs import check_source
            spec = dict(source=self.record.path, identity=[self.record.device, self.record.inode,
                                                          self.record.size, self.record.modified_ns])
            check_source(spec)
            if self.isInterruptionRequested():
                return
            if self.key[0] == "Video":
                from .video import probe, select_streams
                from .tool_paths import resolve_tool
                ffprobe = resolve_tool("ffprobe")
                if not ffprobe:
                    raise ValueError("FFprobe is required to preview video dimensions.")
                info = probe(self.record.path, {"ffprobe": ffprobe}, self.isInterruptionRequested)
                size = select_streams(info, 0)[0]
            else:
                from PIL import Image
                from .images import inspect_image
                with Image.open(self.record.path) as image:
                    size = inspect_image(image)["output_size"]
            check_source(spec)
        except Exception as exc:
            error = str(exc)
        if not self.isInterruptionRequested():
            self.ready.emit(self.key, size, error)


class ResizeControls(QObject):
    def __init__(self, host):
        super().__init__(host)
        self.host, self.worker, self.cache, self.closing = host, None, None, False
        self.tool, self.states = host.tool, {}
        self.panel = QFrame()
        layout = QVBoxLayout(self.panel)
        layout.setVerticalSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addWidget(QLabel("Resize"))
        self.mode = QComboBox()
        for label, value in (("Original size", "none"), ("Width & height", "dimensions"), ("Percentage", "percent")):
            self.mode.addItem(label, value)
        self.mode.setAccessibleName("Resize mode")
        layout.addWidget(self.mode)
        self.dimensions = QFrame()
        row = QHBoxLayout(self.dimensions)
        row.setVerticalSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        row.setContentsMargins(0, 0, 0, 0)
        self.width, self.height = QSpinBox(), QSpinBox()
        for name, control, value in (("Width", self.width, 1920), ("Height", self.height, 1080)):
            column = QVBoxLayout()
            column.setAlignment(Qt.AlignmentFlag.AlignTop)
            column.addWidget(QLabel(name))
            control.setRange(1, 40000)
            control.setValue(value)
            control.setAccessibleName(f"Resize {name.lower()} in pixels")
            control.setToolTip(f"{name} in pixels. With aspect ratio locked, fit inside this box.")
            # Keep five-digit values readable in the narrow settings panel.
            control.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
            control.setStyleSheet("QSpinBox {padding-right: 6px;}")
            column.addWidget(control)
            row.addLayout(column)
        layout.addWidget(self.dimensions)
        self.percent = QSpinBox()
        self.percent.setRange(1, 400)
        self.percent.setValue(50)
        self.percent.setSuffix(" %")
        self.percent.setAccessibleName("Resize percentage")
        layout.addWidget(self.percent)
        self.aspect = QCheckBox("Keep aspect ratio")
        self.aspect.setChecked(True)
        self.aspect.setToolTip("Fit inside width and height. Unlocking can stretch the image.")
        self.no_upscale = QCheckBox("Don't upscale")
        self.no_upscale.setChecked(True)
        self.no_upscale.setToolTip("Never increase either dimension. Smaller images keep their size when aspect ratio is locked.")
        layout.addWidget(self.aspect)
        layout.addWidget(self.no_upscale)
        self.preview = QLabel()
        self.preview.setWordWrap(True)
        self.preview.setTextFormat(Qt.TextFormat.PlainText)
        self.preview.setObjectName("muted")
        layout.addWidget(self.preview)
        host.settings_layout.insertWidget(host.settings_layout.indexOf(host.detail_title), self.panel)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(120)
        self.timer.timeout.connect(self.refresh)
        for control, signal in ((self.mode, "currentIndexChanged"), (self.width, "valueChanged"),
                                (self.height, "valueChanged"), (self.percent, "valueChanged"),
                                (self.aspect, "toggled"), (self.no_upscale, "toggled")):
            getattr(control, signal).connect(self.changed)
        self.update_visibility()

    def options(self):
        return dict(resize_mode=self.mode.currentData(), resize_width=self.width.value(),
                    resize_height=self.height.value(), resize_percent=self.percent.value(),
                    resize_aspect=self.aspect.isChecked(), resize_no_upscale=self.no_upscale.isChecked())

    def set_tool(self, title):
        if title != self.tool:
            self.states[self.tool] = self.options()
            self.tool = title
            values = self.states.get(title, dict(resize_mode="none", resize_width=1920, resize_height=1080,
                resize_percent=50, resize_aspect=True, resize_no_upscale=True))
            controls = (self.mode, self.width, self.height, self.percent, self.aspect, self.no_upscale)
            blockers = [QSignalBlocker(control) for control in controls]
            self.mode.setCurrentIndex(self.mode.findData(values["resize_mode"]))
            for key, control in (("resize_width", self.width), ("resize_height", self.height), ("resize_percent", self.percent)):
                control.setValue(values[key])
            self.aspect.setChecked(values["resize_aspect"])
            self.no_upscale.setChecked(values["resize_no_upscale"])
            del blockers
            if self.worker:
                self.worker.requestInterruption()
        self.mode.setAccessibleName(f"{title} resize mode")
        self.aspect.setEnabled(title != "Video")
        self.aspect.setToolTip("Video keeps the display aspect ratio; dimensions round down to even pixels."
                               if title == "Video" else "Fit inside width and height. Unlocking can stretch the image.")
        self.no_upscale.setToolTip("Never enlarge beyond the normalized display size. Rotated and non-square pixels are normalized first."
                                   if title == "Video" else "Never increase either dimension. Smaller images keep their size when aspect ratio is locked.")
        self.update_visibility()

    def update_visibility(self):
        mode = self.mode.currentData()
        formats = getattr(self.host, "video_format_controls", None)
        remux = self.host.tool == "Video" and formats is not None and formats.action.currentData() == "remux"
        self.panel.setVisible(self.host.tool in ("Images", "Video") and not remux)
        if remux and self.worker:
            self.worker.requestInterruption()
        self.dimensions.setVisible(mode == "dimensions")
        self.aspect.setVisible(mode == "dimensions")
        self.percent.setVisible(mode == "percent")
        self.no_upscale.setVisible(mode != "none")
        self.preview.setVisible(mode != "none")

    def changed(self, *args):
        self.update_visibility()
        self.host.invalidate_plan()
        self.schedule()

    def schedule(self):
        if not self.closing:
            self.timer.start()

    def set_enabled(self, enabled):
        self.panel.setEnabled(enabled)
        if not enabled and self.worker:
            self.worker.requestInterruption()
        if enabled:
            self.schedule()

    def refresh(self):
        host = self.host
        formats = getattr(host, "video_format_controls", None)
        if host.tool == "Video" and formats is not None and formats.action.currentData() == "remux":
            return
        if self.closing or host._closing or host.editing_busy or host.tool not in ("Images", "Video") or self.mode.currentData() == "none":
            return
        rows = host.table.selectionModel().selectedRows()
        count = len(rows) if rows else host.proxy.rowCount()
        if not count:
            self.preview.setText("Add a video to preview dimensions." if host.tool == "Video" else "Add an image to preview dimensions.")
            return
        index = rows[0] if rows else host.proxy.index(0, 0)
        record = host.model.items[host.proxy.mapToSource(index).row()]
        if record.sample:
            self.preview.setText("Add a real file to preview dimensions.")
            return
        key = (host.tool, record.path, record.device, record.inode, record.size, record.modified_ns)
        if self.cache is not None and self.cache[0] == key:
            _, size, error = self.cache
            try:
                if error:
                    raise ValueError(error)
                if host.tool == "Video":
                    from .video_geometry import video_dimensions, display_label_size
                    target = video_dimensions(size, self.mode.currentData(), self.width.value(), self.height.value(),
                                              self.percent.value(), self.aspect.isChecked(), self.no_upscale.isChecked())
                    size = display_label_size(size)
                else:
                    target = resize_dimensions(size, self.mode.currentData(), self.width.value(), self.height.value(),
                                               self.percent.value(), self.aspect.isChecked(), self.no_upscale.isChecked())
                text = f"{size[0]} × {size[1]} → {target[0]} × {target[1]}"
                self.preview.setText((f"First of {count} files\n" if count > 1 else "") + text)
                self.preview.setToolTip(f"{record.name}\n{text}\nFirst of {count} files. Preview changes lists every output size.")
            except Exception as exc:
                self.preview.setText(str(exc))
                self.preview.setToolTip(record.path)
            return
        self.preview.setText("Reading video dimensions…" if host.tool == "Video" else "Reading image dimensions…")
        if self.worker:
            if self.worker.key != key:
                self.worker.requestInterruption()
            return
        self.worker = DimensionWorker(record, key, self)
        self.worker.ready.connect(self.received)
        self.worker.finished.connect(self.finished)
        self.worker.start()

    def received(self, key, size, error):
        self.cache = (key, size, error)

    def finished(self):
        worker, self.worker = self.worker, None
        worker.deleteLater()
        self.schedule()

    def shutdown(self):
        self.closing = True
        self.timer.stop()
        if self.worker:
            self.worker.requestInterruption()
        return self.worker is None
