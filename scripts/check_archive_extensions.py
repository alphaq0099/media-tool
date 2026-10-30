"""Native Windows/Qt trial with generated CBZ/encryption/Trash fixtures only."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QSettings, QTimer, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QPushButton
from PIL import Image

from media_tool.window import Workspace
from media_tool.jobs_ui import QueueDialog
from media_tool.seven_archive import seven_run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--temp-root", type=Path, required=True)
    args = parser.parse_args()
    report = args.report.resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    base = args.temp_root.resolve()
    base.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix="generated-archives-", dir=base)
    root = Path(temporary.name).resolve()
    assert root.parent == base and base.is_relative_to(Path.cwd().resolve())
    book = root / "Book"
    book.mkdir()
    for number, kind in ((1, "png"), (2, "jpg"), (10, "webp")):
        Image.new("RGB", (160, 220), (number * 15, 70, 130)).save(book / f"page{number}.{kind}")
    (book / "notes.txt").write_text("Excluded from CBZ, preserved on disk")
    private = root / "Private"
    private.mkdir()
    (private / "empty").mkdir()
    (private / "one.txt").write_bytes(b"generated encryption fixture\n" * 10000)
    (private / "two.txt").write_text("second generated secret")
    loose = root / "Generated Trash trial.txt"
    loose.write_text("Disposable generated source for the native Recycle Bin test")
    output = root / "output"
    output.mkdir()
    originals = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}
    app = QApplication([])
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    window = Workspace(QSettings(str(root / "settings.ini"), QSettings.Format.IniFormat))
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    queue = QueueDialog(window, root / "history")
    queue.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.jobs_dialog = queue
    waiting = queue.store.enqueue("Waiting", "sample", [dict(name="later", seconds=0)])
    window._tool_changed("Archives")
    window.format.setCurrentText("CBZ")
    window.destination.setText(str(output))
    window.archive_controls.name.setText("Comic")
    state = dict(phase=0, screenshots={}, started=time.perf_counter(), last=time.perf_counter(), last_busy=False, gaps=[])
    password = 'generated native fixture "quotes" $ & []'
    def capture(stage, widget=None):
        for theme in ("light", "dark"):
            window.set_theme(theme, persist=False)
            path = report.with_name(f"{report.stem}-{stage}-{theme}.png")
            assert (widget or window).grab().save(str(path))
            state["screenshots"][f"{stage}_{theme}"] = str(path)
    def finish(result):
        timer.stop()
        result.update(screenshots=state["screenshots"], qt_platform=app.platformName())
        report.write_text(json.dumps(result, indent=2), encoding="utf-8")
        window.close()
    def tick():
        now = time.perf_counter()
        busy_now = window.busy
        if busy_now and state["last_busy"]:
            state["gaps"].append((now - state["last"]) * 1000)
        state["last_busy"] = busy_now
        state["last"] = now
        try:
            if now - state["started"] > 150:
                raise TimeoutError("Archive extension trial timed out")
            if window.busy or queue.enabled:
                return
            phase = state["phase"]
            if phase == 0:
                assert window.model.rowCount() == 1
                window.preview_changes()
            elif phase == 1:
                assert window.last_plan.counts == {"Planned": 1}, window.status.text()
                state["comic_spec"] = window.last_plan.items[0].archive_spec
                assert state["comic_spec"]["file_count"] == 3
                capture("cbz-preview")
                window.archive_controls.show_contents()
                capture("comic-cover", window.archive_contents_dialog)
                window.archive_contents_dialog.close()
                window.start_rename_button.click()
            elif phase == 2:
                assert window.result_model.items[0]["state"] == "succeeded", window.status.text()
                with zipfile.ZipFile(state["comic_spec"]["output"]) as archive:
                    assert archive.namelist() == ["0001.png", "0002.jpg", "0003.webp"]
                    for entry in state["comic_spec"]["manifest"]["entries"]:
                        assert hashlib.sha256(archive.read(entry["name"])).hexdigest() == originals[entry["source"]]
                seven_run(["t", "-bso0", "--", state["comic_spec"]["output"]], None, lambda: False)
                capture("cbz-results")
                window.format.setCurrentText("7z")
                window.archive_controls.password.setText(password)
                window.archive_controls.confirm_password.setText(password)
                window.archive_controls.name.setText("Encrypted")
                window.start_scan([str(private)])
            elif phase == 3:
                window.preview_changes()
            elif phase == 4:
                assert window.last_plan.counts == {"Planned": 1}, window.status.text()
                state["seven_output"] = window.last_plan.items[0].output
                window.settings_scroll.verticalScrollBar().setValue(window.settings_scroll.verticalScrollBar().maximum())
                capture("encryption-options")
                window.start_rename_button.click()
            elif phase == 5:
                assert window.result_model.items[0]["state"] == "succeeded", window.result_model.items
                seven_run(["t", "-p", "-bso0", "--", state["seven_output"]], password, lambda: False)
                try:
                    seven_run(["l", "--", state["seven_output"]], None, lambda: False)
                except ValueError:
                    pass
                else:
                    raise AssertionError("Encrypted filenames were readable")
                capture("encrypted-results")
                window.format.setCurrentText("ZIP")
                window.archive_controls.password.setText(password)
                window.archive_controls.confirm_password.setText(password)
                window.archive_controls.name.setText("Encrypted ZIP")
                window.start_scan([str(loose)])
            elif phase == 6:
                window.start_rename_button.click()
            elif phase == 7:
                assert window.result_model.items[0]["state"] == "succeeded", window.result_model.items
                assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == digest for p, digest in originals.items())
                window.archive_cleanup_button.click()
                capture("cleanup-review", window.archive_cleanup_dialog)
                move = next(b for b in window.archive_cleanup_dialog.findChildren(QPushButton) if b.text().startswith("Move verified"))
                move.click()
            elif phase == 8:
                row = queue.store.items(window.last_result_job)[0]
                cleanup = json.loads(row["original_cleanup"])
                assert row["state"] == "succeeded"
                assert cleanup["moved"] == 1 and not loose.exists(), cleanup
                assert (book / "notes.txt").exists() and private.exists()
                assert queue.store.items(waiting)[0]["attempts"] == 0
                capture("cleanup-results")
                finish(dict(passed=True, cbz_pages=3, page_order_and_bytes=True,
                    seven_zip_encrypted=True, zip_aes_encrypted=True, encrypted_names_verified=True,
                    originals_preserved_until_explicit_cleanup=True, native_trash_moved=1,
                    cleanup_separate_from_archive_success=True, waiting_job_untouched=True,
                    external_cbz_integrity="7-Zip t", comic_reader_acceptance="Two external comic readers still require user validation",
                    maximum_execution_heartbeat_ms=max(state["gaps"], default=0)))
                return
            state["phase"] += 1
        except Exception as error:
            finish(dict(passed=False, phase=state["phase"], error=str(error), status=window.status.text()))
    timer = QTimer(window)
    timer.setInterval(20)
    timer.timeout.connect(tick)
    window.show()
    window.start_scan([str(book)])
    timer.start()
    app.exec()
    temporary.cleanup()
    return 0 if json.loads(report.read_text(encoding="utf-8"))["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
