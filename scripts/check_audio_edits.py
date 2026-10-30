"""Native trim/split/join/fade trial with disposable media and settings."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from PySide6.QtCore import QSettings, QTimer, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QListWidget
from media_tool import __version__
from media_tool.domain import FileRecord
from media_tool.window import Workspace


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = args.report.resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    result = dict(version=__version__, passed=False, qt_platform=app.platformName(), screenshots={}, jobs=[], layouts=[])
    def wait(predicate, seconds=45):
        deadline = time.monotonic() + seconds
        while not predicate():
            if time.monotonic() > deadline:
                raise TimeoutError("Native edit trial timed out")
            app.processEvents()
            time.sleep(.005)
    with tempfile.TemporaryDirectory(prefix="phase9b-native-", dir=report.parent) as scratch:
        base = Path(scratch)
        sources = []
        for name, frequency in (("Track 2 日本.wav", 440), ("Track 10.wav", 880)):
            path = base / name
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000:duration=3",
                "-c:a", "pcm_s24le", str(path)], check=True, capture_output=True, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            sources.append(FileRecord.from_path(path))
        def originals():
            return [(hashlib.sha256(Path(r.path).read_bytes()).hexdigest(), Path(r.path).stat().st_ino,
                     Path(r.path).stat().st_mtime_ns) for r in sources]
        before = originals()
        window = Workspace(QSettings(str(base / "settings.ini"), QSettings.Format.IniFormat))
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        window.show()
        window._tool_changed("Audio")
        window.format.setCurrentText("WAV")
        window.model.append(sources)
        controls = window.audio_controls
        def capture(stage, widget=None):
            for theme in ("light", "dark"):
                window.set_theme(theme, persist=False)
                app.processEvents()
                path = report.with_name(f"{report.stem}-{stage}-{theme}.png")
                assert (widget or window).grab().save(str(path))
                result["screenshots"][stage+"-"+theme] = str(path)
        def process(stage):
            window.preview_button.click()
            wait(lambda: not window.busy)
            assert window.last_plan and all(i.status == "Planned" for i in window.last_plan.items), window.status.text()
            capture(stage+"-preview")
            count = len(window.last_plan.items)
            window.start_rename_button.click()
            wait(lambda: not window.busy and window.tabs.currentIndex() == 2)
            assert len(window.result_model.items) == count
            assert all(i["state"] == "succeeded" for i in window.result_model.items), window.result_model.items
            result["jobs"].append(dict(task=stage, succeeded=count))
            capture(stage+"-results")
        try:
            controls.start.setText("0.5")
            controls.end.setText("2.5")
            controls.ranges.setPlainText("0 - 1\n1 - 2\n2 - end")
            controls.fade_in.setValue(.1)
            controls.fade_out.setValue(.1)
            for width, height in ((1180, 740), (880, 610)):
                window.resize(width, height)
                for task in ("convert", "trim", "split", "join"):
                    controls.task.setCurrentIndex(controls.task.findData(task))
                    window.settings_scroll.verticalScrollBar().setValue(0)
                    capture(f"{task}-{width}")
                    window.settings_scroll.verticalScrollBar().setValue(window.settings_scroll.verticalScrollBar().maximum())
                    capture(f"{task}-options-{width}")
                result["layouts"].append(dict(requested=[width,height], actual=[window.width(),window.height()]))
                assert window.width() == width and window.height() == height
            window.resize(1180, 740)
            controls.task.setCurrentIndex(controls.task.findData("trim"))
            process("trim")
            window.model.append(sources[:1])
            controls.task.setCurrentIndex(controls.task.findData("split"))
            process("split")
            window.model.append(sources)
            controls.task.setCurrentIndex(controls.task.findData("join"))
            def reorder():
                dialog = QApplication.activeModalWidget()
                listing = dialog.findChild(QListWidget)
                item = listing.takeItem(1)
                listing.insertItem(0, item)
                capture("join-order", dialog)
                dialog.accept()
            QTimer.singleShot(100, reorder)
            controls.order_button.click()
            assert controls.join_order == (sources[1].path,sources[0].path)
            process("join")
            queue = window.jobs_dialog
            queue.refresh(window.last_result_job)
            queue.show_archive_results()
            assert window.tabs.currentIndex() == 2
            assert originals() == before
            result.update(passed=True, originals_hash_identity_mtime_preserved=True,
                note="Windows source/native trial; no EXE, clean-machine or Linux claim. All generated media/settings/history removed.")
        except Exception as error:
            result["error"] = repr(error)
        finally:
            window.close()
            wait(lambda: not window.busy and not window.isVisible())
            app.processEvents()
    result["fixture_removed"] = not base.exists()
    report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k:v for k,v in result.items() if k != "screenshots"}, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
