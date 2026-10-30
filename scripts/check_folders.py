"""Native Qt folder trial, screenshots and heartbeat, using disposable fixtures."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from PySide6.QtCore import QSettings, QTimer, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication
from media_tool import __version__
from media_tool.window import Workspace


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--feedback", action="store_true", help="Also capture filtered sizes and invalid-range feedback")
    args = parser.parse_args()
    report = args.report.resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    result = dict(version=__version__, passed=False, qt_platform=app.platformName(), screenshots={}, timings={})
    with tempfile.TemporaryDirectory(prefix="phase8-native-", dir=report.parent) as scratch:
        base = Path(scratch).resolve()
        assert base.is_relative_to(report.parent)
        root = base / "Sample folders"
        for parent, child in (("Parent A", "Set 2"), ("Parent A", "Set 10"), ("Parent B", "Set 2")):
            folder = root / parent / child
            folder.mkdir(parents=True)
            for i in range(1, 7):
                (folder / f"Page {i}.txt").write_bytes(bytes([i]) * (i * 1024))
        (root / "Parent A" / "notes 日本.txt").write_bytes(b"keep or move explicitly")
        (root / "readme.txt").write_bytes(b"already in the root; stays here")
        (root / "Empty folder").mkdir()
        def contents():
            return {p.relative_to(root).as_posix(): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_ino, p.stat().st_mtime_ns)
                    for p in root.rglob("*") if p.is_file()}
        original = contents()
        window = Workspace(QSettings(str(base / "settings.ini"), QSettings.Format.IniFormat))
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        window.show()
        page = window.show_folders()
        ticks = []
        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: ticks.append(time.perf_counter()))
        def wait(predicate, timeout=30):
            deadline = time.monotonic() + timeout
            while not predicate():
                if time.monotonic() > deadline:
                    raise TimeoutError("Folder trial timed out")
                app.processEvents()
                time.sleep(0.002)
            app.processEvents()
        def capture(stage):
            for width, height, size in ((1180, 740, "normal"), (880, 610, "small")):
                window.resize(width, height)
                for theme in ("light", "dark"):
                    window.set_theme(theme, persist=False)
                    app.processEvents()
                    image = report.with_name(f"{report.stem}-{stage}-{size}-{theme}.png")
                    assert window.grab().save(str(image))
                    assert (window.width(), window.height()) == (width, height)
                    assert page.run_button.mapTo(page, page.run_button.rect().bottomRight()).y() < page.height()
                    result["screenshots"][f"{stage}_{size}_{theme}"] = str(image)
        try:
            timer.start()
            started = time.perf_counter()
            page.open_paths([str(root)])
            wait(lambda: not window.busy)
            result["timings"]["scan_ms"] = round((time.perf_counter() - started) * 1000, 2)
            timer.stop()
            assert page.snapshot.complete
            assert page.snapshot.nodes[0].files == 20
            assert window.jobs_dialog is None
            page.tree.expandToDepth(2)
            capture("inspect")
            if args.feedback:
                assert page.minimum.suffix() == page.maximum.suffix() == ""
                page.minimum.setValue(.001)
                wait(lambda: not page.filter_timer.isActive() and not window.busy)
                assert page.tree_model.headerData(1, Qt.Orientation.Horizontal) == "Size (filtered)"
                capture("size-filter")
                page.minimum.setValue(2)
                page.maximum.setValue(1)
                assert page.size_warning.isVisible() and page.worker is None
                capture("range-warning")
                page.minimum.setValue(0)
                page.maximum.setValue(0)
                wait(lambda: not page.filter_timer.isActive() and not window.busy)
            page.operation.setCurrentIndex(1)
            page.collision.setCurrentIndex(1)
            page.loose.setChecked(True)
            page.preview()
            wait(lambda: not window.busy)
            assert page.plan.can_apply and contents() == original
            capture("moves")
            page.preview_tabs.setCurrentIndex(1)
            page.before_tree.expandToDepth(2)
            page.after_tree.expandToDepth(1)
            capture("before-after")
            page.apply()
            wait(lambda: not window.busy)
            assert not (root / "Parent A").exists() and not (root / "Parent B").exists()
            assert (root / "Set 2 (2)").is_dir()
            assert page.result_undoable
            capture("results")
            page.preview_undo()
            wait(lambda: not window.busy)
            assert page.plan.can_apply and not (root / "Parent B").exists()
            capture("undo-preview")
            page.apply()
            wait(lambda: not window.busy)
            assert contents() == original
            result.update(passed=True, round_trip_files=len(original), hashes_identities_timestamps_restored=True,
                          source_bytes=sum(p.stat().st_size for p in root.rglob("*") if p.is_file()),
                          max_scan_heartbeat_gap_ms=round(max(((b-a)*1000 for a,b in zip(ticks, ticks[1:])), default=0), 2),
                          note="Native hidden-window trial, not a clean-machine/package/Linux check. All fixture roots/settings/history are temporary and removed on exit.")
        except Exception as error:
            result["error"] = repr(error)
        finally:
            timer.stop()
            window.close()
            wait(lambda: not window.busy and not window.isVisible())
            app.processEvents()
    result["fixture_removed"] = not base.exists()
    report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "screenshots"}, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
