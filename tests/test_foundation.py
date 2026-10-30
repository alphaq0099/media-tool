import hashlib
import os
from pathlib import Path
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
from PySide6.QtCore import QSettings, Qt, QTimer, QPointF, QMimeData, QUrl
from PySide6.QtGui import QDropEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog
from media_tool.domain import natural_key
from media_tool.services import ScanWorker, ToolProbe
from media_tool.window import Workspace

@pytest.fixture(scope="session")
def app():
    return QApplication.instance() or QApplication([])

def wait_until(predicate, timeout=5000):
    deadline = time.monotonic() + timeout / 1000
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("Qt operation timed out")
        QTest.qWait(10)
        # Let Python worker/pipe-reader threads run as well as Qt's event loop.
        time.sleep(0.001)

@pytest.fixture
def window(app, tmp_path):
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    w = Workspace(settings)
    w.show()
    QTest.qWait(20)
    yield w
    w.close()
    wait_until(lambda: not w.busy and not w.isVisible() and w.waveform.worker is None and w.resize_controls.worker is None)
    QTest.qWait(30)
    w.deleteLater()
    QTest.qWait(10)

def test_theme_persists_and_invalid_value_falls_back(window):
    assert window.theme == 'dark'
    window.set_theme('light')
    window.toggle_theme()
    assert window.theme == "dark"
    settings = QSettings(window.settings.fileName(), QSettings.Format.IniFormat)
    second = Workspace(settings)
    assert second.theme == "dark"
    second.set_theme("garbage")
    assert second.theme == "light"
    second.close()

def test_natural_order_multiple_numeric_groups():
    assert sorted(["a10b2", "a2b10", "a2b2"], key=natural_key) == ["a2b2", "a2b10", "a10b2"]

def test_unicode_duplicate_inputs_and_source_unchanged(window, tmp_path):
    folder = tmp_path / "inputs"
    folder.mkdir()
    source = folder / "holiday [1] \u65e5\u672c.png"
    source.write_bytes(b"test data only")
    nested = folder / "nested"
    nested.mkdir()
    (nested / "x.txt").write_text("nested")
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    window.start_scan([str(folder), str(source)])
    wait_until(lambda: not window.busy)
    assert window.model.rowCount() == 1
    assert window.model.items[0].name == source.name
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    window.recursive.setChecked(True)
    window.start_scan([str(folder)])
    wait_until(lambda: not window.busy)
    assert window.model.rowCount() == 2

def test_missing_input_reported(window, tmp_path):
    window.start_scan([str(tmp_path / "not-there")])
    wait_until(lambda: not window.busy)
    assert window.issues
    assert window.model.rowCount() == 0

def test_symlink_not_followed(window, tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("untouched")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Host does not allow unprivileged symlink creation")
    window.start_scan([str(link)])
    wait_until(lambda: not window.busy)
    assert window.model.rowCount() == 0
    assert "Skipped link" in window.issues[0]

def test_10000_rows_and_event_loop_responsive(window):
    ticks = []
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(time.perf_counter()))
    timer.start()
    window.load_samples()
    wait_until(lambda: not window.busy)
    timer.stop()
    assert window.model.rowCount() == 10000
    assert len(ticks) >= 5
    gaps = [b-a for a,b in zip(ticks,ticks[1:])]
    assert max(gaps, default=0) < 0.5
    assert not window.run_button.isEnabled()

def test_cancellation_retains_only_discovered_rows(window):
    window.start_scan(sample_count=100000)
    QTimer.singleShot(40, window.cancel_scan)
    wait_until(lambda: not window.busy)
    assert window.model.rowCount() < 100000
    assert "Cancelled" in window.status.text()
    assert window.add_button.isEnabled()

def test_close_cancels_worker_without_destroying_running_thread(window):
    window.start_scan(sample_count=100000)
    window.close()
    wait_until(lambda: not window.busy)
    assert not window.isVisible()

def test_picker_routes_into_background_scan(window, monkeypatch, tmp_path):
    path = tmp_path / "file.txt"
    path.write_text("source")
    monkeypatch.setattr('media_tool.input_picker.pick_inputs',lambda *args:[str(path)])
    window.add_files()
    wait_until(lambda: not window.busy)
    assert window.model.items[0].path == str(path)

def test_drop_local_urls_only(window, tmp_path):
    path = tmp_path / "drop.txt"
    path.write_text("source")
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path)), QUrl("https://example.com/file")])
    event = QDropEvent(QPointF(5,5), Qt.DropAction.CopyAction, mime,
                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    window.dropEvent(event)
    wait_until(lambda: not window.busy)
    assert window.model.rowCount() == 1

def test_probe_missing_and_large_stderr(app):
    probe = ToolProbe()
    results = []
    probe.result.connect(results.append)
    probe.start("media-tool-deliberately-missing-executable")
    assert "not found" in results[-1]
    probe.start(sys.executable, ["-c", "import sys; sys.stderr.write('x'*200000); print('finished')"])
    wait_until(lambda: probe.done)
    assert probe.process.exitCode() == 0
    assert len(probe.output) <= 16384

def test_probe_failed_exit(app):
    probe = ToolProbe()
    results = []
    probe.result.connect(results.append)
    probe.start(sys.executable, ["-c", "raise SystemExit(3)"])
    wait_until(lambda: probe.done)
    assert "failed (exit 3)" in results[-1]

def test_probe_timeout_and_cancel(app):
    probe = ToolProbe()
    results = []
    probe.result.connect(results.append)
    probe.start(sys.executable, ["-c", "import time; time.sleep(30)"])
    probe.timer.start(50)
    wait_until(lambda: probe.done)
    wait_until(lambda: probe.process.state() == probe.process.ProcessState.NotRunning)
    assert "timed out" in results[-1]
    probe.start(sys.executable, ["-c", "import time; time.sleep(30)"])
    QTimer.singleShot(50, probe.cancel)
    wait_until(lambda: probe.done)
    wait_until(lambda: probe.process.state() == probe.process.ProcessState.NotRunning)
    assert "cancelled" in results[-1]

def test_theme_updates_header_without_reparsing_global_stylesheet(window, app):
    original_sheet = app.styleSheet()
    for name in ("dark", "light", "dark"):
        window.set_theme(name)
        QTest.qWait(20)
        assert app.styleSheet() == original_sheet
        header_color = window.table.horizontalHeader().grab().toImage().pixelColor(5, 5)
        expected = "#15191f" if name == "dark" else "#dce3dd"
        assert header_color.name() == expected


def test_hidden_controls_refresh_before_show_after_theme_changes(window):
    from PySide6.QtWidgets import QLineEdit
    control = QLineEdit(window)
    control.resize(180, 40)
    for theme in ('light', 'dark', 'light'):
        control.hide()
        window.set_theme(theme)
        control.show()
        QTest.qWait(20)
        expected = '#15191f' if theme == 'dark' else '#dce3dd'
        assert control.grab().toImage().pixelColor(80, 20).name() == expected


def test_worker_sorts_multiple_roots_naturally(window, tmp_path):
    paths = []
    for name in ("file10.txt", "file2.txt", "file1.txt"):
        path = tmp_path / name
        path.write_text(name)
        paths.append(str(path))
    window.start_scan(paths)
    wait_until(lambda: not window.busy)
    assert [r.name for r in window.model.items] == ["file1.txt", "file2.txt", "file10.txt"]
