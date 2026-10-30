"""Source/package smoke diagnostic using disposable generated images only."""
import hashlib
import json
from pathlib import Path
import tempfile
import time

from PySide6.QtCore import QTimer, Qt
from .jobs_ui import QueueDialog


class ImageCheck:
    def __init__(self, window, report, resize=False):
        from PIL import Image, ImageDraw
        self.window, self.report = window, Path(report).resolve()
        self.resize = resize
        self.report.parent.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="mediatool-images-")
        self.root = Path(self.temporary.name)
        self.output = self.root / "converted"
        self.output.mkdir()
        self.before = {}
        for number, extension in enumerate(("png", "jpg", "webp", "bmp", "tiff", "png")):
            path = self.root / f"sample {number + 1} 日本.{extension}"
            size = (4000, 3000) if number == 5 else (960, 640)
            if resize and number in (1, 2):
                size = (640, 960) if number == 1 else (120, 80)
            with Image.new("RGBA" if extension == "png" else "RGB", size, (38, 115, 167)) as image:
                draw = ImageDraw.Draw(image)
                draw.rectangle((size[0] // 5, size[1] // 5, size[0] * 4 // 5, size[1] * 4 // 5), fill=(248, 187, 72))
                draw.ellipse((size[0] // 3, size[1] // 3, size[0] * 2 // 3, size[1] * 2 // 3), fill=(50, 180, 130))
                if extension == "png":
                    draw.rectangle((0, 0, size[0] // 8, size[1]), fill=(255, 0, 0, 0))
                image.save(path)
            self.before[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.queue = QueueDialog(window, self.root / "history")
        window.jobs_dialog = self.queue
        self.queue.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.waiting = self.queue.store.enqueue("Waiting", "sample", [{"name": "later", "seconds": 0}])
        window._tool_changed("Images")
        window.destination.setText(str(self.output))
        window.alpha_background.setCurrentIndex(0)
        if resize:
            window.format.setCurrentText("PNG")
            window.resize_controls.mode.setCurrentIndex(1)
            window.resize_controls.width.setValue(480)
            window.resize_controls.height.setValue(320)
        self.phase, self.shots = 0, {}
        self.started = self.last = time.perf_counter()
        self.gaps = []
        self.timer = QTimer(window)
        self.timer.setInterval(10)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        window.start_scan(list(self.before))

    def screenshots(self, stage):
        for theme in ("light", "dark"):
            self.window.set_theme(theme, persist=False)
            path = self.report.with_name(f"{self.report.stem}-{stage}-{theme}.png")
            assert self.window.grab().save(str(path))
            self.shots[f"{stage}_{theme}"] = str(path)

    def tick(self):
        current = time.perf_counter()
        if self.phase == 2:
            self.gaps.append((current - self.last) * 1000)
        self.last = current
        try:
            if current - self.started > 60:
                raise TimeoutError("Image diagnostic timed out")
            window, queue = self.window, self.queue
            if self.phase == 0 and not window.busy:
                if self.resize:
                    if not window.resize_controls.preview.text().endswith("960 × 640 → 480 × 320"):
                        return
                    self.screenshots("settings")
                window.preview_changes()
                self.phase = 1
            elif self.phase == 1 and not window.busy:
                assert window.last_plan.counts == {"Planned": 6}, str(window.last_plan)
                assert not list(self.output.iterdir())
                if self.resize:
                    self.expected_sizes = [[480, 320], [213, 320], [120, 80], [480, 320], [480, 320], [427, 320]]
                    assert [item.image_spec["output_size"] for item in window.last_plan.items] == self.expected_sizes
                self.screenshots("preview")
                self.conversion_started = time.perf_counter()
                window.start_rename_button.click()
                self.phase = 2
                self.last = time.perf_counter()
            elif self.phase == 2 and not window.busy and not queue.enabled:
                self.conversion_seconds = time.perf_counter() - self.conversion_started
                assert window.result_model.rowCount() == 6
                assert all(item["state"] == "succeeded" for item in window.result_model.items), window.status.text()
                assert all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest for path, digest in self.before.items())
                assert len(list(self.output.iterdir())) == 6
                if self.resize:
                    from PIL import Image
                    for item, expected in zip(window.result_model.items, self.expected_sizes):
                        with Image.open(item["output"]) as output:
                            output.load()
                            assert list(output.size) == expected
                assert queue.store.items(self.waiting)[0]["attempts"] == 0
                assert not queue.isVisible()
                assert window.model.rowCount() == window.plan_model.rowCount() == 0
                assert not window.result_can_undo
                window.results_table.selectRow(0)
                self.screenshots("results")
                window.compare_button.click()
                window.comparison_dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
                self.phase = 3
            elif self.phase == 3 and window.comparison_dialog.worker is None:
                dialog = window.comparison_dialog
                assert len(dialog.previews) == 2, dialog.note.text()
                path = self.report.with_name(f"{self.report.stem}-comparison.png")
                assert dialog.grab().save(str(path))
                self.shots["comparison"] = str(path)
                dialog.close()
                self.finish(dict(passed=True, files=6, largest_pixels=12_000_000,
                    original_hashes_unchanged=True, output_decodes_verified=True,
                    preview_read_only=True, waiting_job_untouched=True, comparison_loaded=True,
                    resizing=self.resize, expected_sizes=getattr(self, "expected_sizes", None),
                    conversion_seconds=self.conversion_seconds,
                    execution_heartbeat_max_ms=max(self.gaps, default=0), screenshots=self.shots))
        except Exception as error:
            self.finish(dict(passed=False, phase=self.phase, error=str(error) or type(error).__name__))

    def finish(self, result):
        self.timer.stop()
        self.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        self.window.close()
