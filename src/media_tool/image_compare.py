"""On-demand, bounded image previews; decoding stays outside the GUI thread."""
from pathlib import Path
from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QDialog, QLabel, QHBoxLayout, QVBoxLayout, QPushButton, QSizePolicy


class CompareWorker(QThread):
    ready = Signal(object)
    failed = Signal(str)

    def __init__(self, item, parent):
        super().__init__(parent)
        self.item = dict(item)

    def run(self):
        try:
            from PIL import Image
            from .images import inspect_image, normalized_pixels
            previews = []
            for key in ("source", "output"):
                if self.isInterruptionRequested():
                    return
                path = Path(self.item[key])
                with Image.open(path) as image:
                    info = inspect_image(image)
                    image.draft("RGB", (720, 480))
                    image.thumbnail((720, 480))
                    oriented, pixels, _ = normalized_pixels(image)
                    try:
                        rgba = pixels.convert("RGBA")
                        try:
                            preview = QImage(rgba.tobytes(), rgba.width, rgba.height,
                                             QImage.Format.Format_RGBA8888).copy()
                        finally:
                            rgba.close()
                    finally:
                        pixels.close()
                        oriented.close()
                w, h = info["output_size"]
                previews.append((preview, f"{path.name}\n{info['format']} · {w} × {h} · {path.stat().st_size:,} bytes"))
            if not self.isInterruptionRequested():
                self.ready.emit(previews)
        except Exception as error:
            self.failed.emit(str(error))


class CompareDialog(QDialog):
    def __init__(self, item, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        if parent.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen):
            self.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.setWindowTitle("Compare images")
        self.setModal(True)
        self.resize(1000, 600)
        self.closing = False
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.labels, self.details = [], []
        for title in ("Original", "Converted"):
            column = QVBoxLayout()
            column.addWidget(QLabel(title))
            picture = QLabel("Loading…")
            picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
            picture.setMinimumSize(100, 100)
            picture.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
            picture.setStyleSheet("background: #808080;")
            detail = QLabel()
            detail.setTextFormat(Qt.TextFormat.PlainText)
            detail.setWordWrap(True)
            detail.setMinimumHeight(detail.fontMetrics().lineSpacing() * 3)
            column.addWidget(picture, 1)
            column.addWidget(detail)
            row.addLayout(column, 1)
            self.labels.append(picture)
            self.details.append(detail)
        layout.addLayout(row, 1)
        self.note = QLabel("Scaled previews on grey. Use the original files for full-resolution inspection.")
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        layout.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        self.previews = []
        self.worker = CompareWorker(item, self)
        self.worker.ready.connect(self.show_previews)
        self.worker.failed.connect(self.note.setText)
        self.worker.finished.connect(self.finished_loading)
        self.worker.start()

    def show_previews(self, previews):
        self.previews = previews
        for label, (_, detail) in zip(self.details, previews):
            label.setText(detail)
        self.fit_previews()

    def fit_previews(self):
        for label, (image, _) in zip(self.labels, self.previews):
            label.setPixmap(QPixmap.fromImage(image).scaled(label.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                                           Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit_previews()

    def finished_loading(self):
        worker, self.worker = self.worker, None
        worker.deleteLater()
        if self.closing:
            self.close()

    def reject(self):
        self.close()

    def closeEvent(self, event):
        if self.worker is not None:
            self.closing = True
            self.worker.requestInterruption()
            self.note.setText("Closing after the current image decoder returns…")
            event.ignore()
        else:
            event.accept()
