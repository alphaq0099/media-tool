"""Capture shared workspace layout and archive password controls using native Qt."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QLabel
from media_tool.window import Workspace
from media_tool import __version__


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--settings", type=Path, required=True)
    args = parser.parse_args()
    report = args.report.resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    window = Workspace(QSettings(str(args.settings.resolve()), QSettings.Format.IniFormat))
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.show()
    shots, layouts = {}, []
    def capture(stage):
        for theme in ("light", "dark"):
            window.set_theme(theme, persist=False)
            app.processEvents()
            path = report.with_name(f"{report.stem}-{stage}-{theme}.png")
            assert window.grab().save(str(path))
            shots[f"{stage}_{theme}"] = str(path)
    try:
        for width, height, size in ((1180, 740, "normal"), (880, 610, "small")):
            window.resize(width, height)
            for tool in ("Images", "Video", "Rename", "Archives"):
                window._tool_changed(tool)
                window.tabs.setCurrentIndex(0)
                window.settings_scroll.verticalScrollBar().setValue(0)
                app.processEvents()
                capture(f"{tool.lower()}-{size}")
                panel = window.file_layout.parentWidget()
                count_y = window.count_label.mapTo(panel, window.count_label.rect().bottomLeft()).y()
                layouts.append(dict(tool=tool, size=size, actual_size=[window.width(), window.height()],
                    panel_top=panel.mapTo(window, panel.rect().topLeft()).y(),
                    count_bottom_gap=panel.height() - count_y, tagline=window.subtitle.text(),
                    settings_scroll_max=window.settings_scroll.verticalScrollBar().maximum()))
                assert not window.status.text() and not window.preview_summary.text()
                assert not window.preview_reason.isVisible()
                assert not any(label.text() == "Configure this operation" for label in window.findChildren(QLabel))
                assert panel.height() - count_y < 30
        window.resize(1180, 740)
        window._tool_changed("Archives")
        controls = window.archive_controls
        assert [window.format.itemText(i) for i in range(window.format.count())] == ["7z", "ZIP", "CBZ"]
        controls.password.setText("generated fixture only")
        controls.confirm_password.setText("generated fixture only")
        assert controls.encrypt_names.isChecked() and controls.confirm_password.isVisible()
        window.settings_scroll.verticalScrollBar().setValue(window.settings_scroll.verticalScrollBar().maximum())
        app.processEvents()
        capture("archive-options-bottom")
        controls.password.clear()
        controls.confirm_password.clear()
        controls.set_password_visible(True)
        app.processEvents()
        assert not controls.confirm_password.isVisible() and not controls.options()["archive_encrypt"]
        capture("archive-eye-open-empty")
        for kind in ("ZIP", "CBZ"):
            window.format.setCurrentText(kind)
            window.settings_scroll.verticalScrollBar().setValue(0)
            app.processEvents()
            capture("archive-" + kind.lower())
        result = dict(passed=True, version=__version__, qt_platform=app.platformName(),
            screenshots=shots, layouts=layouts, password_not_recorded=True,
            note="Native off-screen captures; generated masked password only. No media or user settings modified.")
    except Exception as error:
        result = dict(passed=False, error=str(error), screenshots=shots, layouts=layouts)
    finally:
        window.close()
        app.processEvents()
    report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
