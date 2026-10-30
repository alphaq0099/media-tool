"""Native audio workspace trial using disposable fixtures, settings and job history."""
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
from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication
from media_tool import __version__
from media_tool.audio import audio_info, inspect_tools
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
    result = dict(version=__version__, passed=False, platform=app.platformName(), screenshots={}, layouts=[])
    def wait(predicate, seconds=40):
        deadline = time.monotonic() + seconds
        while not predicate():
            if time.monotonic() > deadline:
                raise TimeoutError("Native audio trial timed out")
            app.processEvents()
            time.sleep(.005)
    with tempfile.TemporaryDirectory(prefix="phase9a-native-", dir=report.parent) as scratch:
        base = Path(scratch)
        source = base / "Soundtracks 日本.mkv"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=size=48x32:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000",
            "-map", "0:v", "-map", "1:a", "-map", "2:a", "-t", "1", "-c:v", "libx264", "-threads", "1", "-c:a", "aac",
            "-metadata:s:a:0", "language=eng", "-metadata:s:a:1", "title=Second soundtrack", str(source)],
            check=True, capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        original = hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_ino, source.stat().st_mtime_ns
        window = Workspace(QSettings(str(base / "settings.ini"), QSettings.Format.IniFormat))
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        window.show()
        def capture(stage):
            for theme in ("light", "dark"):
                window.set_theme(theme, persist=False)
                app.processEvents()
                path = report.with_name(f"{report.stem}-{stage}-{theme}.png")
                assert window.grab().save(str(path))
                result["screenshots"][stage + "-" + theme] = str(path)
        try:
            window._tool_changed("Audio")
            assert window.jobs_dialog is None and window.audio_controls.worker is None
            window.start_scan([str(source)])
            wait(lambda: not window.busy)
            for width, height in ((1180, 740), (880, 610)):
                window.resize(width, height)
                app.processEvents()
                for name in ("MP3", "M4A / AAC", "FLAC", "WAV", "Opus"):
                    window.format.setCurrentText(name)
                    window.settings_scroll.verticalScrollBar().setValue(0)
                    capture(f"{name.split(' ')[0].lower()}-{width}")
                    if name in ("MP3", "FLAC"):
                        window.settings_scroll.verticalScrollBar().setValue(window.settings_scroll.verticalScrollBar().maximum())
                        capture(f"{name.lower()}-options-{width}")
                for control in (*window.nav_group.buttons(), window.theme_button):
                    pos = control.mapTo(window, control.rect().topLeft())
                    assert pos.y() >= 0 and pos.y() + control.height() <= window.height(), (control.text(), pos.y(), window.height())
                result["layouts"].append(dict(requested=[width, height], actual=[window.width(), window.height()],
                                               sidebar_controls_inside_window=True))
                window.format.setCurrentText("MP3")
                window.audio_controls.mode.setCurrentIndex(1)
                capture(f"copy-{width}")
                window.audio_controls.mode.setCurrentIndex(0)
            window.resize(1180, 740)
            window.format.setCurrentText("FLAC")
            window.preview_button.click()
            wait(lambda: not window.busy)
            assert window.last_plan.counts == {"Planned": 2}, window.last_plan
            outputs = [i.output for i in window.last_plan.items]
            capture("preview-two-tracks")
            queue = window.ensure_jobs()
            window.start_rename_button.click()
            wait(lambda: queue.worker is None and window.tabs.currentIndex() == 2)
            assert len(window.result_model.items) == 2
            assert all(i["state"] == "succeeded" for i in window.result_model.items), window.result_model.items
            window.results_table.selectRow(0)
            capture("results-two-tracks")
            tools = inspect_tools("flac")
            infos = [audio_info(path, tools) for path in outputs]
            assert all(i["streams"][0]["codec_name"] == "flac" for i in infos)
            assert (hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_ino, source.stat().st_mtime_ns) == original
            window.show_folders()
            page = window.folders_page
            page.operation.setCurrentIndex(1)
            assert not hasattr(page, "size_scope_hint")
            capture("folders-explanation-removed")
            result.update(passed=True, verified_outputs=len(outputs), original_hash_identity_mtime_unchanged=True,
                          no_tools_started_on_audio_navigation=True,
                          note="Native Windows source trial. No EXE rebuild; no Linux or clean-machine validation.")
        except Exception as error:
            result["error"] = repr(error)
        finally:
            window.close()
            wait(lambda: not window.busy and not window.isVisible())
            app.processEvents()
    result["fixture_removed"] = not base.exists()
    report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "screenshots"}, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
