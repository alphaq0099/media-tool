"""Opt-in rename/undo diagnostic using only generated files and isolated history."""
import hashlib
import json
from pathlib import Path
import tempfile
import time

from PySide6.QtCore import QTimer, Qt
from .jobs_ui import QueueDialog


class UndoCheck:
    def __init__(self, window, report):
        self.window, self.report = window, Path(report).resolve()
        self.report.parent.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="mediatool-undo-")
        self.root = Path(self.temporary.name)
        self.folder = self.root / "files"
        self.folder.mkdir()
        self.before = {}
        for index in range(20):
            suffix = (".TXT", ".png", ".mkv")[index % 3]
            path = self.folder / f"original {index + 1} 日本{suffix}"
            path.write_bytes(f"disposable original {index}\n".encode() * 50)
            self.before[str(path)] = self.fingerprint(path)
        self.queue = QueueDialog(window, self.root / "history")
        window.jobs_dialog = self.queue
        self.queue.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.waiting = self.queue.store.enqueue("Waiting sample", "sample", [{"name": "later", "seconds": 0}])
        self.phase = 0
        self.started = self.last = time.perf_counter()
        self.gaps, self.execution_gaps = [], []
        self.shots = {}
        self.timer = QTimer(window)
        self.timer.setInterval(10)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        window.start_scan(list(self.before))

    @staticmethod
    def fingerprint(path):
        info = path.stat()
        return (hashlib.sha256(path.read_bytes()).hexdigest(), info.st_ino, info.st_size, info.st_mtime_ns)

    def screenshots(self, stage):
        for theme in ("light", "dark"):
            self.window.set_theme(theme, persist=False)
            path = self.report.with_name(f"{self.report.stem}-{stage}-{theme}.png")
            assert self.window.grab().save(str(path))
            self.shots[f"{stage}_{theme}"] = str(path)

    def tick(self):
        current = time.perf_counter()
        self.gaps.append((current - self.last) * 1000)
        if self.phase in (1, 5):
            self.execution_gaps.append((current - self.last) * 1000)
        self.last = current
        try:
            if current - self.started > 60:
                raise TimeoutError("Undo diagnostic timed out")
            window, queue = self.window, self.queue
            if self.phase == 0 and not window.busy:
                window._tool_changed("Rename")
                window.start_rename_button.click()
                self.phase = 1
            elif self.phase == 1 and not window.busy and not queue.enabled:
                self.rename_job = window.last_result_job
                assert all(item["state"] == "succeeded" for item in queue.store.items(self.rename_job))
                assert not queue.isVisible()
                self.outputs = [Path(item["output"]) for item in window.result_model.items]
                assert all(path.exists() for path in self.outputs)
                assert queue.shutdown()
                self.queue = QueueDialog(window, self.root / "history")
                window.jobs_dialog = self.queue
                self.queue.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
                self.conflict = Path(next(iter(self.before)))
                self.conflict.write_bytes(b"unrelated arriving file")
                assert window.preview_undo(self.rename_job)
                self.phase = 2
            elif self.phase == 2 and not window.busy:
                assert not window.undo_plan.can_apply
                assert not window.apply_undo_button.isEnabled()
                assert self.conflict.read_bytes() == b"unrelated arriving file"
                assert all(path.exists() for path in self.outputs)
                assert queue.store.undo_job(self.rename_job) is None
                self.screenshots("conflict")
                self.conflict.unlink()
                window.discard_undo_button.click()
                window.preview_undo_button.click()
                self.phase = 3
            elif self.phase == 3 and not window.busy:
                assert window.undo_plan.can_apply
                assert not any(Path(path).exists() for path in self.before)
                assert len(queue.store.jobs()) == 2
                self.screenshots("preview")
                assert window.apply_undo_button.isEnabled()
                window.apply_undo_button.click()
                self.undo_job = queue.store.undo_job(self.rename_job)
                assert self.undo_job
                self.phase = 5
                self.last = time.perf_counter()
            elif self.phase == 5 and not window.busy and not queue.enabled:
                assert all(item["state"] == "succeeded" for item in queue.store.items(self.undo_job))
                assert all(self.fingerprint(Path(path)) == identity for path, identity in self.before.items())
                assert len(list(self.folder.iterdir())) == 20
                assert not list(self.folder.glob("*.stage"))
                assert not queue.isVisible()
                assert queue.store.items(self.waiting)[0]["attempts"] == 0
                assert window.model.rowCount() == window.plan_model.rowCount() == 0
                assert window.result_model.rowCount() == 20
                assert window.tabs.currentIndex() == 2
                self.screenshots("results")
                jobs = queue.store.jobs()
                assert queue.shutdown()
                self.queue = QueueDialog(window, self.root / "history")
                window.jobs_dialog = self.queue
                self.queue.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
                assert self.queue.store.jobs() == jobs
                assert self.queue.store.undo_job(self.rename_job) == self.undo_job
                self.finish(dict(passed=True, files=20, exact_names_contents_and_identity_restored=True,
                                 preview_read_only=True, conflict_blocked=True, history_survived_reopen=True,
                                 undo_link_survived_reopen=True, queue_not_opened=True, waiting_job_untouched=True,
                                 results_in_workspace=True, stage_files_remaining=0,
                                 execution_heartbeat_max_ms=max(self.execution_gaps, default=0),
                                 callback_max_ms_including_screenshots=max(self.gaps), screenshots=self.shots))
        except Exception as error:
            self.finish(dict(passed=False, phase=self.phase, error=str(error) or type(error).__name__))

    def finish(self, result):
        self.timer.stop()
        self.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        self.window.close()
