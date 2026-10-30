"""Video compression goals; quality remains the default."""
from PySide6.QtCore import QObject, Qt
from PySide6.QtWidgets import QFrame, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QDoubleSpinBox, QCheckBox, QLayout


class CompressionControls(QObject):
    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.panel = QFrame()
        layout = QVBoxLayout(self.panel)
        layout.setVerticalSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.setSpacing(8)
        layout.addWidget(QLabel("Compression"))
        self.mode = QComboBox()
        self.mode.addItem("Quality", "quality")
        self.mode.addItem("Target file size", "target_size")
        self.mode.setAccessibleName("Video compression mode")
        self.mode.setToolTip("Quality targets retained detail. Target file size budgets video/audio for a maximum size per output and uses two passes.")
        layout.addWidget(self.mode)
        self.target_panel = QFrame()
        target_layout = QVBoxLayout(self.target_panel)
        target_layout.setVerticalSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        target_layout.setContentsMargins(0, 0, 0, 0)
        target_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        target_layout.setSpacing(8)
        target_layout.addWidget(QLabel("Maximum size per file"))
        row = QHBoxLayout()
        self.amount = QDoubleSpinBox()
        self.amount.setRange(0.01, 1_000_000)
        self.amount.setDecimals(2)
        self.amount.setValue(10)
        self.amount.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        self.amount.setAccessibleName("Maximum output size per video")
        self.amount.setToolTip("A maximum for each file, not the batch total. Actual size is verified; tight budgets can reduce detail.")
        self.unit = QComboBox()
        self.unit.addItems(["MB", "MiB"])
        self.unit.setAccessibleName("Target size units")
        self.unit.setToolTip("MB = 1,000,000 bytes. MiB = 1,048,576 bytes.")
        row.addWidget(self.amount, 1)
        row.addWidget(self.unit)
        target_layout.addLayout(row)
        target_layout.addWidget(QLabel("Audio bitrate per track"))
        self.audio = QComboBox()
        for rate in (64, 96, 128, 192, 256, 320):
            self.audio.addItem(f"{rate} kb/s", rate)
        self.audio.setCurrentIndex(self.audio.findData(128))
        self.audio.setAccessibleName("Target size audio bitrate")
        self.audio.setToolTip("Each selected AAC track shares the size budget at this bitrate. More audio tracks leave less room for video. Ignored for silent clips or No audio.")
        target_layout.addWidget(self.audio)
        self.reuse = QCheckBox("Reuse analysis on retry")
        self.reuse.setToolTip("Keep only a completed first analysis pass if interrupted. Retry skips that pass only when source, settings, encoder and saved logs match. Logs are removed on success; this does not resume partway through a file.")
        target_layout.addWidget(self.reuse)
        layout.addWidget(self.target_panel)
        host.settings_layout.insertWidget(host.settings_layout.indexOf(host.detail_title), self.panel)
        for control, signal in ((self.mode, "currentIndexChanged"), (self.amount, "valueChanged"),
                                (self.unit, "currentIndexChanged"), (self.audio, "currentIndexChanged")):
            getattr(control, signal).connect(self.changed)
        self.update_visibility()
        self.reuse.toggled.connect(self.changed)

    def options(self):
        return dict(video_mode=self.mode.currentData(), video_target_size=self.amount.value(),
                    video_target_unit=self.unit.currentText(), video_audio_kbps=self.audio.currentData(),
                    video_reuse_analysis=self.reuse.isChecked())

    def update_visibility(self):
        video = self.host.tool == "Video"
        target = self.mode.currentData() == "target_size"
        formats = getattr(self.host, "video_format_controls", None)
        remux = formats is not None and formats.action.currentData() == "remux"
        self.panel.setVisible(video and not remux)
        self.target_panel.setVisible(target)
        if video:
            self.host.detail.setVisible(not target and not remux)
            self.host.detail_title.setVisible(not target and not remux)

    def changed(self, *args):
        self.update_visibility()
        self.host.invalidate_plan()
