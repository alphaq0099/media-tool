"""Separate container and video codec controls without eager tool discovery."""
from PySide6.QtCore import QObject
from PySide6.QtWidgets import QFrame, QVBoxLayout, QLabel, QComboBox

from .video_formats import CODECS, COMPATIBLE


class VideoFormatControls(QObject):
    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self._standard_codec = "h264"
        self.panel = QFrame()
        layout = QVBoxLayout(self.panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("Processing"))
        self.action = QComboBox()
        self.action.addItem("Convert video", "convert")
        self.action.addItem("Remux (copy streams)", "remux")
        self.action.setAccessibleName("Video processing method")
        layout.addWidget(self.action)
        self.codec_label = QLabel("Video codec")
        layout.addWidget(self.codec_label)
        self.codec = QComboBox()
        for key, info in CODECS.items():
            self.codec.addItem(info["label"], key)
        self.codec.setAccessibleName("Video codec")
        self.codec.setToolTip("The codec compresses video; the container is the output file format. WebM uses VP9/Opus; MP4 and MKV use H.264 or H.265 with AAC.")
        layout.addWidget(self.codec)
        self.note = QLabel()
        self.note.setObjectName("muted")
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        host.settings_layout.insertWidget(host.settings_layout.indexOf(host.format) + 1, self.panel)
        self.codec.currentIndexChanged.connect(self.changed)
        self.action.currentIndexChanged.connect(self.changed)
        host.format.currentIndexChanged.connect(self.changed)
        self.update_visibility()

    def update_visibility(self):
        video = self.host.tool == "Video"
        remux = self.action.currentData() == "remux"
        if video and not remux:
            allowed = COMPATIBLE.get(self.host.format.currentText().lower(), ("h264", "hevc"))
            selected = self.codec.currentData()
            if selected in ("h264", "hevc"):
                self._standard_codec = selected
            self.codec.blockSignals(True)
            try:
                for index in range(self.codec.count()):
                    self.codec.model().item(index).setEnabled(self.codec.itemData(index) in allowed)
                if selected not in allowed:
                    self.codec.setCurrentIndex(self.codec.findData("vp9" if "vp9" in allowed else self._standard_codec))
            finally:
                self.codec.blockSignals(False)
        hevc = self.codec.currentData() == "hevc"
        vp9 = self.codec.currentData() == "vp9"
        self.panel.setVisible(video)
        self.codec.setVisible(not remux)
        self.codec_label.setVisible(not remux)
        self.note.setVisible(remux or hevc or vp9)
        self.note.setText("Copy video/audio unchanged.\nNo resize or target size.\nUse compatible tracks.\nSubtitles can be added." if remux else
                          "WebM uses VP9/Opus.\nQuality mode only.\nFor a size target, use MP4/MKV\nwith H.264 or H.265." if vp9 else
                          "H.265 needs more processing\nand HEVC playback support.\nQuality or Target file size\ncan be used with MP4/MKV.")
        mode = self.host.compression_controls.mode
        mode.model().item(mode.findData("target_size")).setEnabled(not vp9)
        if not remux and vp9 and mode.currentData() == "target_size":
            mode.setCurrentIndex(mode.findData("quality"))
        self.host.compression_controls.update_visibility()
        self.host.resize_controls.update_visibility()
        if hasattr(self.host, "video_speed_label"):
            self.host.video_speed_label.setVisible(not remux)
            self.host.video_speed.setVisible(not remux)

    def changed(self, *args):
        if not getattr(self.host, "_planning_ready", False):
            return
        self.update_visibility()
        self.host.invalidate_plan()
