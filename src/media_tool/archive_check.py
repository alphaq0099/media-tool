"""Native ZIP workspace diagnostic using only generated inputs and isolated history."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import time
import zipfile

from PySide6.QtCore import QTimer, Qt
from .jobs_ui import QueueDialog
from .tool_paths import resolve_tool


class ArchiveCheck:
    def __init__(self, window, report):
        self.window, self.report = window, Path(report).resolve()
        self.report.parent.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="mediatool-zip-")
        self.root = Path(self.temporary.name)
        album = self.root / "Inputs" / "Album 日本"
        (album / "empty").mkdir(parents=True)
        (album / "page 2.txt").write_bytes(b"archive page\n" * 100000)
        (album / "skip.tmp").write_bytes(b"omitted temporary file")
        (album / ".hidden").write_bytes(b"omitted hidden file")
        self.paths = [str(album)]
        for index, name in enumerate(("one", "two"), 1):
            folder = self.root / "Inputs" / name
            folder.mkdir()
            file = folder / "same.txt"
            file.write_bytes(f"generated source {index}\n".encode() * 100000)
            self.paths.append(str(file))
        self.before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.root / "Inputs").rglob("*") if p.is_file()}
        self.output = self.root / "output"
        self.output.mkdir()
        queue = self.queue = QueueDialog(window, self.root / "history")
        window.jobs_dialog = queue
        queue.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.waiting = queue.store.enqueue("Waiting", "sample", [dict(name="later", seconds=0)])
        window._tool_changed("Archives")
        window.format.setCurrentText("ZIP")
        window.destination.setText(str(self.output))
        controls = window.archive_controls
        controls.name.setText("Selection 日本")
        controls.level.setCurrentIndex(controls.level.findData(1))
        controls.exclusions.setText("*.tmp")
        controls.hidden.setChecked(False)
        self.started = self.last = time.perf_counter()
        self.phase, self.shots, self.gaps = 0, {}, []
        self.timer = QTimer(window)
        self.timer.setInterval(10)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        window.start_scan(self.paths)

    def screenshots(self, stage, widget=None):
        widget = widget or self.window
        for theme in ("light", "dark"):
            self.window.set_theme(theme, persist=False)
            if stage == "options":
                self.window.settings_scroll.verticalScrollBar().setValue(self.window.settings_scroll.verticalScrollBar().maximum())
            path = self.report.with_name(f"{self.report.stem}-{stage}-{theme}.png")
            assert widget.grab().save(str(path))
            self.shots[f"{stage}_{theme}"] = str(path)

    def tick(self):
        now = time.perf_counter()
        if self.phase in (2, 4):
            self.gaps.append((now - self.last) * 1000)
        self.last = now
        window = self.window
        try:
            if now - self.started > 100:
                raise TimeoutError("ZIP workspace trial timed out")
            if self.phase == 0 and not window.busy:
                assert window.model.rowCount() == 3
                assert any(record.kind == "Folder" for record in window.model.items)
                assert not window.keep.isVisible() and not window.recursive.isVisible()
                window.preview_changes()
                self.phase = 1
            elif self.phase == 1 and not window.busy:
                assert window.last_plan.counts == {"Planned": 1}, window.status.text()
                spec = window.last_plan.items[0].archive_spec
                assert spec["file_count"] == 3 and len(spec["manifest"]["excluded"]) == 2
                self.screenshots("combined-preview")
                self.screenshots("options")
                window.settings_scroll.verticalScrollBar().setValue(0)
                window.archive_contents_button.click()
                self.screenshots("contents", window.archive_contents_dialog)
                window.archive_contents_dialog.close()
                self.combined_started = time.perf_counter()
                window.start_rename_button.click()
                self.phase, self.last = 2, time.perf_counter()
            elif self.phase == 2 and not window.busy and not self.queue.enabled:
                assert window.result_model.rowCount() == 1 and window.result_model.items[0]["state"] == "succeeded", window.status.text()
                self.combined_seconds = time.perf_counter() - self.combined_started
                output = Path(window.result_model.items[0]["output"])
                with zipfile.ZipFile(output) as archive:
                    assert archive.testzip() is None
                    self.combined_names = archive.namelist()
                    assert "same.txt" in self.combined_names and "same (2).txt" in self.combined_names
                    assert "Album 日本/empty/" in self.combined_names
                    assert all(not name.endswith(("skip.tmp", ".hidden")) for name in self.combined_names)
                tool = resolve_tool("7z")
                self.external_verifier = None
                if tool:
                    result = subprocess.run([tool, "t", str(output)], capture_output=True, timeout=30,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    assert result.returncode == 0, result.stdout.decode(errors="replace")[-2000:]
                    self.external_verifier = "7-Zip t"
                self.screenshots("combined-results")
                controls = window.archive_controls
                controls.mode.setCurrentIndex(controls.mode.findData("separate"))
                controls.level.setCurrentIndex(controls.level.findData(0))
                assert not controls.name.isVisible()
                window.start_scan(self.paths)
                self.phase = 3
            elif self.phase == 3 and not window.busy:
                self.screenshots("separate-mode")
                self.separate_started = time.perf_counter()
                window.start_rename_button.click()
                self.phase, self.last = 4, time.perf_counter()
            elif self.phase == 4 and not window.busy and not self.queue.enabled:
                assert window.result_model.rowCount() == 3
                assert all(item["state"] == "succeeded" for item in window.result_model.items), window.status.text()
                assert self.queue.store.items(self.waiting)[0]["attempts"] == 0
                assert window.model.rowCount() == window.plan_model.rowCount() == 0
                assert not window.result_can_undo and not self.queue.isVisible()
                for path, digest in self.before.items():
                    assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
                for item in window.result_model.items:
                    with zipfile.ZipFile(item["output"]) as archive:
                        assert archive.testzip() is None and all(info.compress_type == zipfile.ZIP_STORED for info in archive.infolist())
                assert len(list(self.output.glob("*.zip"))) == 4
                window.results_table.selectRow(0)
                self.screenshots("separate-results")
                self.finish(dict(passed=True, combined=True, separate_archives=3, output_archives=4,
                    duplicate_basenames_preserved=True, empty_folder_preserved=True, exclusions_verified=True,
                    manifest_preview_verified=True, originals_unchanged=True, waiting_job_untouched=True,
                    full_contents_crc_and_sha256_verified=True, external_verifier=self.external_verifier,
                    combined_entries=self.combined_names, combined_seconds=self.combined_seconds,
                    separate_seconds=time.perf_counter() - self.separate_started,
                    execution_heartbeat_max_ms=max(self.gaps, default=0), screenshots=self.shots))
        except Exception as error:
            self.finish(dict(passed=False, phase=self.phase, error=str(error) or type(error).__name__, status=window.status.text()))

    def finish(self, result):
        self.timer.stop()
        self.report.write_text(json.dumps(result, indent=2), encoding="utf-8")
        self.window.close()
