"""Opt-in UI-to-filesystem diagnostic; all renamed files are generated fixtures."""
import hashlib
import json
from pathlib import Path
import tempfile
import time

from PySide6.QtCore import QTimer, Qt
from .jobs_ui import QueueDialog
from .services import ToolProbe


class RenameCheck:
    def __init__(self, window, report):
        self.window = window
        self.report = Path(report).resolve()
        self.report.parent.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="mediatool-rename-")
        root = Path(self.temporary.name)
        self.folder = root / "files"
        self.folder.mkdir()
        self.before = {}
        for number in range(1, 21):
            extension = (".TXT", ".png", ".mkv")[(number - 1) % 3]
            path = self.folder / f"raw {number} 日本{extension}"
            path.write_bytes(f"disposable original {number}\n".encode() * 100)
            info = path.stat()
            self.before[str(path)] = (hashlib.sha256(path.read_bytes()).hexdigest(),
                                      info.st_size, info.st_mtime_ns, info.st_ino)
        self.conflicts = []
        for name in ("Item_0001.TXT", "Item_0002.png"):
            path = self.folder / name
            path.write_bytes(b"existing file kept")
            self.conflicts.append(path)
        self.queue = QueueDialog(window, root / "history")
        window.jobs_dialog = self.queue
        self.queue.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.waiting_job = self.queue.store.enqueue("Waiting sample", "sample", [{"name": "not started", "seconds": 0}])
        self.phase = 0
        self.gaps = []
        self.queue_gaps = []
        self.started = self.last = time.perf_counter()
        self.timer = QTimer(window)
        self.timer.setInterval(10)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        window.start_scan(list(self.before))

    def tick(self):
        current = time.perf_counter()
        self.gaps.append((current - self.last) * 1000)
        if self.phase == 2:
            self.queue_gaps.append((current - self.last) * 1000)
        self.last = current
        try:
            if current - self.started > 30:
                raise TimeoutError("Rename diagnostic timed out")
            window, queue = self.window, self.queue
            if self.phase == 0 and not window.busy:
                window._tool_changed("Rename")
                window.prefix.setText("Item_")
                window.start_number.setValue(1)
                assert window.preview_changes()
                self.phase = 1
            elif self.phase == 1 and not window.busy:
                assert window.last_plan.counts == {"Planned": 20}
                assert len(list(self.folder.iterdir())) == 22
                self.outputs = {item.source.path: item.output for item in window.last_plan.items}
                for theme in ("light", "dark"):
                    window.set_theme(theme)
                    assert window.grab().save(str(self.report.with_name(self.report.stem + f"-preview-{theme}.png")))
                assert window.start_rename_button.isEnabled()
                window.start_rename_button.click()
                assert len(queue.store.jobs()) == 2
                assert not queue.isVisible()
                self.rename_job = next(job["id"] for job in queue.store.jobs() if job["adapter"] == "rename")
                self.phase = 2
                self.last = time.perf_counter()
            elif self.phase == 2 and queue.worker is None and not queue.enabled:
                assert next(job["state"] for job in queue.store.jobs() if job["id"] == self.rename_job) == "succeeded"
                assert queue.store.items(self.waiting_job)[0]["state"] == "queued"
                assert queue.store.items(self.waiting_job)[0]["attempts"] == 0
                assert not queue.isVisible()
                assert len(list(self.folder.iterdir())) == 22
                for source, target in self.outputs.items():
                    output = Path(target)
                    info = output.stat()
                    actual = (hashlib.sha256(output.read_bytes()).hexdigest(), info.st_size, info.st_mtime_ns, info.st_ino)
                    assert actual == self.before[source]
                    assert not Path(source).exists()
                assert all(path.read_bytes() == b"existing file kept" for path in self.conflicts)
                assert window.model.rowCount() == 0
                assert window.plan_model.rowCount() == 0
                assert {item["output"] for item in window.result_model.items} == set(self.outputs.values())
                assert window.tabs.currentIndex() == 2
                assert not list(self.folder.glob("*.stage"))
                jobs = queue.store.jobs()
                directory = queue.directory
                assert queue.shutdown()
                queue.hide()
                self.queue = QueueDialog(window, directory)
                window.jobs_dialog = self.queue
                self.queue.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
                assert self.queue.store.jobs() == jobs
                assert not self.queue.enabled
                self.phase = 3
            elif self.phase == 3:
                shots = {}
                self.queue.show()
                self.queue.refresh(self.rename_job)
                for theme in ("light", "dark"):
                    self.window.set_theme(theme)
                    path = self.report.with_name(self.report.stem + f"-results-{theme}.png")
                    assert self.window.grab().save(str(path))
                    shots[theme] = str(path)
                self.result = {"passed": True, "renamed_files": 20, "existing_files_preserved": 2,
                               "contents_and_identity_preserved": True, "preview_did_not_mutate": True,
                               "file_list_cleared": True, "preview_cleared": True,
                               "results_in_workspace": True, "history_survived_reopen": True,
                               "direct_rename": True, "queue_window_was_not_opened": True,
                               "waiting_job_stayed_queued": True,
                               "heartbeat_max_ms": max(self.queue_gaps, default=0),
                               "screenshots": shots}
                self.tool_probe = ToolProbe(self.window)
                self.tool_message = ""
                self.tool_probe.result.connect(self.tool_received)
                self.phase = 4
                self.tool_probe.start("7z")
            elif self.phase == 4 and self.tool_probe.done and self.tool_probe.process.state() == self.tool_probe.process.ProcessState.NotRunning:
                self.result["seven_zip"] = {
                    "found": "Location:" in self.tool_message,
                    "program": self.tool_probe.resolved_program,
                    "message": self.tool_message,
                }
                self.result["all_callback_gap_max_ms_including_screenshot_work"] = max(self.gaps)
                self.finish(self.result)
        except Exception as error:
            self.finish({"passed": False, "phase": self.phase, "error": str(error) or type(error).__name__})

    def finish(self, result):
        self.timer.stop()
        self.report.write_text(json.dumps(result, indent=2), encoding="utf-8")
        self.window._rename_check = self
        self.window.close()

    def tool_received(self, message):
        self.tool_message = message
