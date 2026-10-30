"""Compact audio settings; probing and track inspection run in background workers."""
from PySide6.QtCore import QThread, Signal, Qt, QSignalBlocker
from PySide6.QtWidgets import (QFrame, QVBoxLayout, QComboBox, QLabel, QPushButton, QDialog,
                               QDialogButtonBox, QListWidget, QListWidgetItem, QAbstractItemView,
                               QLineEdit, QPlainTextEdit, QDoubleSpinBox, QHBoxLayout, QFileDialog)


class TrackWorker(QThread):
    ready = Signal(object)
    failed = Signal(str)

    def __init__(self, path, parent):
        super().__init__(parent)
        self.path = path

    def run(self):
        try:
            from .audio import audio_info, inspect_tools
            tools = inspect_tools("wav", True, self.isInterruptionRequested)
            info = audio_info(self.path, tools, self.isInterruptionRequested)
            if not self.isInterruptionRequested():
                self.ready.emit(info["streams"])
        except Exception as error:
            if not self.isInterruptionRequested():
                self.failed.emit(str(error))


class AudioControls:
    def __init__(self, host):
        self.host = host
        self.worker = None
        self.chosen = ""
        self.copying = False
        self.encode_shape = (0, 0, 0., 0.)
        self.join_order = ()
        self.panel = QFrame()
        layout = QVBoxLayout(self.panel)
        layout.setContentsMargins(0, 0, 0, 0)
        def combo(title, choices):
            label = QLabel(title)
            control = QComboBox()
            control.setAccessibleName(title)
            for text, data in choices:
                control.addItem(text, data)
            layout.addWidget(label)
            layout.addWidget(control)
            control.currentIndexChanged.connect(self.changed)
            return label, control
        _, self.task = combo("Task", [("Convert / extract", "convert"), ("Trim", "trim"), ("Split", "split"), ("Join", "join"), ("Volume", "volume"), ("Normalize loudness", "normalize"), ("Tags & cover", "tags")])
        _, self.mode = combo("Processing", [("Convert audio", "encode"), ("Copy encoded audio", "copy")])
        self.mode.setToolTip("Convert supports every edit and fades. Copy edits support MP3/M4A only, snap trims to packet boundaries, and can retain encoder padding at joins. Copy joining requires matching standalone audio files; use Convert for other formats or mixed inputs.")
        _, self.tracks = combo("Audio tracks", [("All tracks", "all"), ("First track", "single"), ("Choose tracks", "custom")])
        self.choose = QPushButton("Choose tracks…")
        self.choose.clicked.connect(self.choose_tracks)
        self.choose.setToolTip("Inspect the highlighted file, or the first visible file. Chosen track positions apply to each input; missing tracks block that input.")
        layout.addWidget(self.choose)
        self.selected = QLabel("")
        self.selected.setWordWrap(True)
        layout.addWidget(self.selected)
        self.trim_panel = QFrame()
        trim = QVBoxLayout(self.trim_panel)
        trim.setContentsMargins(0, 0, 0, 0)
        self.start = QLineEdit("0")
        self.end = QLineEdit()
        self.end.setPlaceholderText("End of track")
        for name, control in (("Start", self.start), ("End", self.end)):
            trim.addWidget(QLabel(name))
            trim.addWidget(control)
            control.setAccessibleName("Audio " + name.lower())
            control.setToolTip("Seconds, MM:SS or HH:MM:SS; for example 90 or 01:30.5. Times are relative to the selected audio track.")
            control.textChanged.connect(self.changed)
        layout.addWidget(self.trim_panel)
        self.split_panel = QFrame()
        split = QVBoxLayout(self.split_panel)
        split.setContentsMargins(0, 0, 0, 0)
        split.addWidget(QLabel("Ranges (start - end)"))
        self.ranges = QPlainTextEdit()
        self.ranges.setAccessibleName("Audio split ranges")
        self.ranges.setPlaceholderText("0 - 00:30\n00:30 - 01:00\n01:00 - end")
        self.ranges.setFixedHeight(110)
        self.ranges.setToolTip("One range per line, in time order without overlaps. Use seconds or MM:SS. End can be 'end'. Each range creates a separate file.")
        self.ranges.textChanged.connect(self.changed)
        split.addWidget(self.ranges)
        layout.addWidget(self.split_panel)
        self.order_button = QPushButton("Join order…")
        self.order_button.clicked.connect(self.choose_order)
        self.order_button.setToolTip("Join highlighted inputs, or all visible inputs. Default order is natural filename order. Choose exactly one track per input.")
        layout.addWidget(self.order_button)
        self.order_summary = QLabel("")
        self.order_summary.setWordWrap(True)
        layout.addWidget(self.order_summary)
        self.fade_panel = QFrame()
        fade_layout = QVBoxLayout(self.fade_panel)
        fade_layout.setContentsMargins(0, 0, 0, 0)
        self.fade_in, self.fade_out = QDoubleSpinBox(), QDoubleSpinBox()
        for title, control in (("Fade in (seconds)", self.fade_in), ("Fade out (seconds)", self.fade_out)):
            control.setRange(0, 3600)
            control.setDecimals(3)
            control.setSingleStep(.1)
            control.setAccessibleName(title)
            control.setToolTip("0 disables this fade. Fades apply to each trimmed/split output, or to the whole joined output, and require conversion.")
            control.valueChanged.connect(self.changed)
            fade_layout.addWidget(QLabel(title))
            fade_layout.addWidget(control)
        layout.addWidget(self.fade_panel)
        self.level_panel = QFrame()
        level_layout = QVBoxLayout(self.level_panel)
        level_layout.setContentsMargins(0, 0, 0, 0)
        self.gain, self.lufs, self.true_peak = QDoubleSpinBox(), QDoubleSpinBox(), QDoubleSpinBox()
        self.level_fields = []
        for title, control, low, high, value in (("Gain (dB)", self.gain, -60, 24, 0), ("Target loudness (LUFS)", self.lufs, -70, -5, -16), ("True-peak limit (dBTP)", self.true_peak, -9, 0, -1.5)):
            control.setRange(low, high)
            control.setDecimals(1)
            control.setSingleStep(.5)
            control.setValue(value)
            control.setAccessibleName(title)
            title_label = QLabel(title)
            level_layout.addWidget(title_label)
            level_layout.addWidget(control)
            self.level_fields.append((title_label, control))
            control.valueChanged.connect(self.changed)
        layout.addWidget(self.level_panel)
        self.tag_panel = QFrame()
        tag_layout = QVBoxLayout(self.tag_panel)
        tag_layout.setContentsMargins(0, 0, 0, 0)
        self.tag_mode = QComboBox()
        for text, data in (("Keep supported tags", "keep"), ("Edit supplied fields", "edit"), ("Remove tags", "remove")):
            self.tag_mode.addItem(text, data)
        tag_layout.addWidget(QLabel("Tags"))
        tag_layout.addWidget(self.tag_mode)
        self.tag_fields = {}
        self.tag_edit = QFrame()
        tag_edit_layout = QVBoxLayout(self.tag_edit)
        tag_edit_layout.setContentsMargins(0, 0, 0, 0)
        for key in ("title", "artist", "album", "track", "date", "genre"):
            control = QLineEdit()
            control.setPlaceholderText("Keep existing if blank")
            control.setAccessibleName("Audio tag " + key)
            control.setMaxLength(512)
            tag_edit_layout.addWidget(QLabel("Year / date" if key == "date" else key.title()))
            tag_edit_layout.addWidget(control)
            control.textChanged.connect(self.changed)
            self.tag_fields[key] = control
        tag_layout.addWidget(self.tag_edit)
        self.cover_mode = QComboBox()
        for text, data in (("Keep cover", "keep"), ("Replace cover", "replace"), ("Remove cover", "remove")):
            self.cover_mode.addItem(text, data)
        tag_layout.addWidget(QLabel("Artwork"))
        tag_layout.addWidget(self.cover_mode)
        self.cover_path = QLineEdit()
        self.cover_path.setPlaceholderText("JPEG / PNG cover")
        self.cover_path.setAccessibleName("Cover image")
        self.cover_browse = QPushButton("Choose cover…")
        def choose_cover():
            path, _ = QFileDialog.getOpenFileName(host, "Choose cover", self.cover_path.text(), "Images (*.jpg *.jpeg *.png)")
            if path:
                self.cover_path.setText(path)
        self.cover_browse.clicked.connect(choose_cover)
        tag_layout.addWidget(self.cover_path)
        tag_layout.addWidget(self.cover_browse)
        self.tag_mode.currentIndexChanged.connect(self.changed)
        self.cover_mode.currentIndexChanged.connect(self.changed)
        self.cover_path.textChanged.connect(self.changed)
        layout.addWidget(self.tag_panel)
        self.rate_label, self.rate_mode = combo("Encoding", [("Quality (VBR)", "quality"), ("Bitrate", "bitrate")])
        self.rate_mode.setToolTip("Quality uses a variable bitrate for MP3; bitrate requests a constant MP3 rate. AAC and Opus use bitrate targets, not exact final file sizes.")
        self.quality_label, self.quality = combo("Quality", [("Low", 7), ("Balanced", 4), ("High", 2), ("Best", 0)])
        self.quality.setCurrentIndex(2)
        self.bitrate_label, self.bitrate = combo("Bitrate (kb/s)", [(str(v), v) for v in (32, 48, 64, 96, 128, 160, 192, 256, 320)])
        self.bitrate.setCurrentIndex(6)
        _, self.sample_rate = combo("Sample rate", [("Automatic", 0)] + [(f"{v} Hz", v) for v in (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000, 88200, 96000, 192000)])
        self.sample_rate.setToolTip("Automatic keeps the source rate where supported. MP3 caps it at 48 kHz, AAC at 96 kHz; Opus uses 48 kHz. Copy keeps the source rate.")
        _, self.channels = combo("Channels", [("Original channels", "preserve"), ("Mono", "mono"), ("Stereo", "stereo")])
        policy = QLabel("Originals kept; tags/artwork removed.")
        self.policy = policy
        policy.setWordWrap(True)
        policy.setToolTip("Each chosen track becomes a separate file. Tags & cover manages supported metadata; other tasks remove metadata and artwork. FLAC/WAV cannot restore detail lost in a lossy source. WAV uses 24-bit PCM; FLAC keeps up to 24-bit precision.")
        layout.addWidget(policy)
        host.settings_layout.insertWidget(host.settings_layout.indexOf(host.keep), self.panel)
        host.format.currentIndexChanged.connect(self.changed)

    def options(self):
        copying = self.mode.currentData() == "copy"
        return dict(audio_mode=self.mode.currentData(), audio_tracks_mode=self.tracks.currentData(), audio_tracks=self.chosen,
                    audio_rate_mode=self.rate_mode.currentData(), audio_quality=self.quality.currentData(),
                    audio_bitrate=self.bitrate.currentData(), audio_sample_rate=0 if copying else self.sample_rate.currentData(),
                    audio_channels="preserve" if copying else self.channels.currentData(), audio_action=self.task.currentData(),
                    audio_start=self.start.text(), audio_end=self.end.text(), audio_ranges=self.ranges.toPlainText(),
                    audio_join_order=self.join_order,
                    audio_fade_in=0 if self.task.currentData() in ("volume", "normalize", "tags") else self.fade_in.value(),
                    audio_fade_out=0 if self.task.currentData() in ("volume", "normalize", "tags") else self.fade_out.value(),
                    audio_gain=self.gain.value(), audio_lufs=self.lufs.value(), audio_true_peak=self.true_peak.value(),
                    audio_tag_mode=self.tag_mode.currentData(), audio_cover_mode=self.cover_mode.currentData(),
                    audio_cover_path=self.cover_path.text(), **{"audio_tag_"+k:v.text() for k,v in self.tag_fields.items()})

    def changed(self, *args):
        # Signals fire while the form and host are still being constructed.
        if not hasattr(self, "channels"):
            return
        copying = self.mode.currentData() == "copy"
        if copying != self.copying:
            blockers = [QSignalBlocker(c) for c in (self.sample_rate, self.channels, self.fade_in, self.fade_out)]
            if copying:
                self.encode_shape = (self.sample_rate.currentIndex(), self.channels.currentIndex(), self.fade_in.value(), self.fade_out.value())
                self.sample_rate.setCurrentIndex(0)
                self.channels.setCurrentIndex(0)
                self.fade_in.setValue(0)
                self.fade_out.setValue(0)
            else:
                self.sample_rate.setCurrentIndex(self.encode_shape[0])
                self.channels.setCurrentIndex(self.encode_shape[1])
                self.fade_in.setValue(self.encode_shape[2])
                self.fade_out.setValue(self.encode_shape[3])
            self.copying = copying
            del blockers
        self.update_visibility()
        if getattr(self.host, "_planning_ready", False):
            self.host.invalidate_plan()

    def update_visibility(self):
        self.panel.setVisible(self.host.tool == "Audio")
        copying = self.mode.currentData() == "copy"
        mp3 = self.host.format.currentText() == "MP3"
        lossy = self.host.format.currentText() in ("MP3", "M4A / AAC", "Opus")
        for item in (self.rate_label, self.rate_mode):
            item.setVisible(not copying and mp3)
        for item in (self.quality_label, self.quality):
            item.setVisible(not copying and mp3 and self.rate_mode.currentData() == "quality")
        for item in (self.bitrate_label, self.bitrate):
            item.setVisible(not copying and lossy and (not mp3 or self.rate_mode.currentData() == "bitrate"))
        self.choose.setVisible(self.tracks.currentData() == "custom")
        self.selected.setVisible(self.tracks.currentData() == "custom" and bool(self.chosen))
        self.selected.setText("Tracks: " + self.chosen)
        self.sample_rate.setEnabled(not copying)
        self.channels.setEnabled(not copying)
        task = self.task.currentData()
        self.mode.setEnabled(task not in ('volume','normalize','tags'))
        self.fade_panel.setVisible(not copying and task not in ("volume", "normalize", "tags"))
        self.level_panel.setVisible(task in ("volume", "normalize"))
        for index, pair in enumerate(self.level_fields):
            for widget in pair:
                widget.setVisible((index == 0) == (task == "volume"))
        if task in ("volume", "normalize") and copying:
            self.mode.setCurrentIndex(self.mode.findData("encode"))
        if task == "tags" and not copying:
            self.mode.setCurrentIndex(self.mode.findData("copy"))
        self.tag_panel.setVisible(task == "tags")
        self.tag_edit.setVisible(self.tag_mode.currentData() == "edit")
        self.cover_path.setVisible(self.cover_mode.currentData() == "replace")
        self.cover_browse.setVisible(self.cover_mode.currentData() == "replace")
        self.policy.setText("Originals kept; six supported tags and one cover." if task == "tags" else "Originals kept; tags/artwork removed.")
        self.trim_panel.setVisible(task == "trim")
        self.split_panel.setVisible(task == "split")
        self.order_button.setVisible(task == "join")
        self.order_summary.setVisible(task == "join" and bool(self.join_order))
        self.order_summary.setText(f"{len(self.join_order)} inputs ordered")
        if task == "join" and self.tracks.currentData() == "all":
            self.tracks.setCurrentIndex(self.tracks.findData("single"))

    def choose_order(self):
        from pathlib import Path
        host = self.host
        if host.editing_busy:
            return
        rows = host.table.selectionModel().selectedRows()
        records = ([host.model.items[host.proxy.mapToSource(row).row()] for row in rows] if rows else
                   [r for r in host.model.items if host.proxy.spec.matches(r)])
        paths = [r.path for r in sorted(records, key=lambda r: r.sort_key)]
        if len(paths) < 2:
            host.status.setText("Add or highlight at least two inputs to arrange their join order.")
            return
        order = [p for p in self.join_order if p in paths] + [p for p in paths if p not in self.join_order]
        dialog = QDialog(host)
        dialog.setWindowTitle(getattr(self,"order_title","Audio join order"))
        dialog.resize(560, 350)
        layout = QVBoxLayout(dialog)
        listing = QListWidget()
        listing.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        for path in order:
            item = QListWidgetItem(Path(path).name)
            item.setData(Qt.ItemDataRole.UserRole, path)
            item.setToolTip(path)
            listing.addItem(item)
        listing.setCurrentRow(0)
        layout.addWidget(listing)
        moves = QHBoxLayout()
        def move(delta):
            current = listing.currentRow()
            if 0 <= current + delta < listing.count():
                item = listing.takeItem(current)
                listing.insertItem(current + delta, item)
                listing.setCurrentRow(current + delta)
        for title, delta in (("Move up", -1), ("Move down", 1)):
            button = QPushButton(title)
            button.clicked.connect(lambda checked=False, d=delta: move(d))
            moves.addWidget(button)
        layout.addLayout(moves)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.join_order = tuple(listing.item(i).data(Qt.ItemDataRole.UserRole) for i in range(listing.count()))
            self.changed()
        dialog.deleteLater()

    def choose_tracks(self):
        host = self.host
        if host.editing_busy or self.worker:
            return
        rows = host.table.selectionModel().selectedRows()
        record = (host.model.items[host.proxy.mapToSource(rows[0]).row()] if rows else
                  next((r for r in host.model.items if host.proxy.spec.matches(r)), None))
        if record is None or record.sample:
            host.status.setText("Add an audio or video file, then choose its tracks.")
            return
        self.worker = TrackWorker(record.path, host)
        self.worker.ready.connect(self.show_tracks)
        self.worker.failed.connect(host.status.setText)
        self.worker.finished.connect(self.finished)
        host.status.setText("Inspecting audio tracks…")
        host.update_controls()
        self.worker.start()

    def finished(self):
        worker, self.worker = self.worker, None
        worker.deleteLater()
        self.host.update_controls()
        if self.host._closing:
            self.host.close()

    def show_tracks(self, streams):
        if self.host._closing:
            return
        if not streams:
            self.host.status.setText("This file has no audio tracks.")
            return
        dialog = QDialog(self.host)
        dialog.setWindowTitle("Choose audio tracks")
        dialog.resize(540, 300)
        layout = QVBoxLayout(dialog)
        listing = QListWidget()
        listing.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        chosen = set(self.chosen.split(","))
        for ordinal, stream in enumerate(streams, 1):
            description = " / ".join(str(v) for v in (stream["codec_name"], stream.get("language"), stream.get("title"),
                                      stream.get("channel_layout") or f"{stream['channels']} channels") if v)
            item = QListWidgetItem(f"Track {ordinal}: {description}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if str(ordinal) in chosen else Qt.CheckState.Unchecked)
            listing.addItem(item)
        layout.addWidget(listing)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.chosen = ",".join(str(i+1) for i in range(listing.count()) if listing.item(i).checkState() == Qt.CheckState.Checked)
            self.changed()
            self.host.status.setText("Track selection saved." if self.chosen else "Choose at least one audio track before processing.")
        dialog.deleteLater()
