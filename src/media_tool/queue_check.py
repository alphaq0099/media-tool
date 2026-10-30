"""Opt-in packaged diagnostic using disposable generated files only."""
import hashlib
import json
from pathlib import Path
import tempfile
import time

from PySide6.QtCore import QTimer, Qt
from .domain import FileRecord
from .jobs import copy_snapshot
from .jobs_ui import QueueDialog


class QueueCheck:
    def __init__(self, window, report):
        self.window = window
        self.report = Path(report).resolve()
        self.report.parent.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="mediatool-phase2-")
        root = Path(self.temporary.name)
        source = root / "source 日本 [1].txt"
        source.write_bytes(b"disposable copy verification fixture\n" * 10000)
        output = root / "output"
        output.mkdir()
        self.source = source
        self.before = source.stat()
        self.digest = hashlib.sha256(source.read_bytes()).hexdigest()
        self.queue = QueueDialog(window, root / "history")
        window.jobs_dialog = self.queue
        self.queue.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.queue.show()
        self.failed = self.queue.store.enqueue("Retry sample", "sample", [
            {"name": "First", "seconds": 0.1},
            {"name": "Fails once", "seconds": 0.1, "fail_once": True}])
        self.cancelled = self.queue.store.enqueue("Cancel sample", "sample", [
            {"name": "Cancel this", "seconds": 1.5},
            {"name": "Waiting", "seconds": 0.1}])
        self.copied = self.queue.store.enqueue("Verified copy", "copy",
            copy_snapshot([FileRecord.from_path(source)], output))
        self.output = output / source.name
        self.queue.refresh(self.failed)
        self.phase = 0
        self.started = self.last = time.perf_counter()
        self.gaps = []
        self.timer = QTimer(window)
        self.timer.setInterval(10)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.queue.start_queue()
        # File intake remains independent of the immutable queued manifest.
        window.start_scan(sample_count=10000)

    def tick(self):
        current = time.perf_counter()
        self.gaps.append((current - self.last) * 1000)
        self.last = current
        try:
            if current - self.started > 25:
                raise TimeoutError("Queue diagnostic timed out")
            queue = self.queue
            if self.phase == 0:
                if queue.worker and queue.worker.job_id == self.cancelled:
                    queue.worker.cancelled.set()
                if queue.worker is None and not queue.enabled:
                    assert queue.store.items(self.failed)[1]["state"] == "failed"
                    assert all(item["state"] == "cancelled" for item in queue.store.items(self.cancelled))
                    assert queue.store.items(self.copied)[0]["state"] == "succeeded"
                    assert queue.store.retry(self.failed) == 1
                    assert queue.store.retry(self.cancelled) == 2
                    queue.start_queue()
                    self.phase = 1
            elif self.phase == 1 and queue.worker is None and not queue.enabled and not self.window.busy:
                assert all(job["state"] == "succeeded" for job in queue.store.jobs())
                assert [item["attempts"] for item in queue.store.items(self.failed)] == [1, 2]
                assert hashlib.sha256(self.output.read_bytes()).hexdigest() == self.digest
                assert self.source.read_bytes() == self.output.read_bytes()
                after = self.source.stat()
                assert (after.st_size, after.st_mtime_ns, after.st_ino) == (
                    self.before.st_size, self.before.st_mtime_ns, self.before.st_ino)
                assert self.window.model.rowCount() == 10000
                history = queue.store.jobs()
                directory = queue.directory
                assert queue.shutdown()
                queue.hide()
                self.queue = QueueDialog(self.window, directory)
                self.window.jobs_dialog = self.queue
                self.queue.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
                assert self.queue.store.jobs() == history
                assert not self.queue.enabled
                self.phase = 2
            elif self.phase == 2:
                shots = {}
                for theme in ("light", "dark"):
                    self.window.set_theme(theme)
                    self.window.grab().save(str(self.report.with_name(self.report.stem + f"-workspace-{theme}.png")))
                    self.queue.show()
                    self.queue.refresh(self.failed)
                    path = self.report.with_name(self.report.stem + f"-queue-{theme}.png")
                    assert self.queue.grab().save(str(path))
                    shots[theme] = str(path)
                self.finish({"passed": True, "source_preserved": True, "copy_sha256": self.digest,
                             "history_survived_reopen": True, "sample_rows": 10000,
                             "jobs": self.queue.store.jobs(),
                             "retry_attempts": [item["attempts"] for item in self.queue.store.items(self.failed)],
                             "heartbeat_max_ms": max(self.gaps), "screenshots": shots})
        except Exception as error:
            self.finish({"passed": False, "error": str(error), "phase": self.phase})

    def finish(self, result):
        self.timer.stop()
        self.report.write_text(json.dumps(result, indent=2), encoding="utf-8")
        # Keep TemporaryDirectory alive until the queue thread has stopped.
        self.window._queue_check = self
        self.window.close()
