"""Application lifecycle; diagnostics use only disposable generated media."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

def main():
    if len(sys.argv) == 3 and sys.argv[1] == '--acceptance-check':
        from .acceptance_check import run
        return run(Path(sys.argv[2]))
    if sys.argv[1:] == ['--archive-worker']:
        from .archive_worker import main as archive_main
        try:
            return archive_main()
        except Exception:
            return 1
    if sys.argv[1:] == ["--media-worker"]:
        from .media_worker import main as media_main
        return media_main()
    if sys.argv[1:] == ["--document-worker"]:
        from .document_worker import main as document_main
        return document_main()
    parser = argparse.ArgumentParser(description="Media Tool - file planning, batch rename and saved jobs")
    parser.add_argument("--settings-file", type=Path)
    parser.add_argument("--queue-check", type=Path, help="Run queue checks with disposable fixtures and save a JSON report")
    parser.add_argument("--rename-check", type=Path, help="Run rename checks with disposable fixtures and save a JSON report")
    parser.add_argument("--undo-check", type=Path, help="Run rename/undo checks with disposable fixtures and save a JSON report")
    parser.add_argument("--image-check", type=Path, help="Convert disposable image fixtures and save a JSON report")
    parser.add_argument("--resize-check", type=Path, help="Resize and convert disposable images and save a JSON report")
    parser.add_argument("--video-check", type=Path, help="Convert disposable videos and save a JSON report")
    parser.add_argument("--compress-check", type=Path, help="Compress disposable videos to byte caps and save a JSON report")
    parser.add_argument("--format-check", type=Path, help="Convert disposable videos to H.265/MKV and save a JSON report")
    parser.add_argument("--webm-check", type=Path, help="Convert disposable videos to VP9/Opus WebM and save a JSON report")
    parser.add_argument("--tracks-check", type=Path, help="Verify multiple audio/subtitle selection with disposable media")
    parser.add_argument("--subtitle-check", type=Path, help="Embed a disposable external subtitle and check native UI")
    parser.add_argument("--remux-check", type=Path, help="Remux generated media, embed a subtitle and check native UI")
    parser.add_argument("--hevc-size-check", type=Path, help="Compress disposable H.265 videos to byte caps with native UI")
    parser.add_argument("--cleanup-check", type=Path, help="Remux a generated fixture and move its original to the native Recycle Bin")
    parser.add_argument("--archive-check", type=Path, help="Create and verify generated ZIPs using native workspace controls")
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--theme", choices=("light", "dark"))
    parser.add_argument("--sample-rows", type=int, default=0)
    parser.add_argument("--benchmark-ready", type=Path)
    parser.add_argument("--exit-after-ms", type=int, default=0)
    options = parser.parse_args()
    from PySide6.QtCore import Qt, QSettings, QTimer
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication
    from .window import Workspace

    app = QApplication(sys.argv[:1])
    app.setApplicationName("Media Tool")
    app.setOrganizationName("MediaTool")
    from .branding import application_icon
    app.setWindowIcon(application_icon())
    if sys.platform=='win32':
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('MediaTool.Desktop')
    from . import __version__
    app.setApplicationVersion(__version__)
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI" if sys.platform == "win32" else "Sans Serif", 10))
    settings = QSettings(str(options.settings_file), QSettings.Format.IniFormat) if options.settings_file else None
    window = Workspace(settings)
    if options.theme:
        window.set_theme(options.theme, persist=False)
    if options.benchmark_ready or options.screenshot or options.queue_check or options.rename_check or options.undo_check or options.image_check or options.resize_check or options.video_check or options.compress_check or options.format_check or options.webm_check or options.tracks_check or options.subtitle_check or options.remux_check or options.hevc_size_check or options.cleanup_check or options.archive_check:
        # Render the real Widgets tree with the native Qt platform, without a
        # visible/activating window. Benchmark reports disclose this limitation.
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    def ready():
        if options.benchmark_ready:
            options.benchmark_ready.parent.mkdir(parents=True, exist_ok=True)
            options.benchmark_ready.write_text(json.dumps({
                "pid": os.getpid(), "ready_perf_counter": time.perf_counter(),
                "mode": "native-platform off-screen first paint + event-loop callback",
                "qt_platform": app.platformName(),
                "loaded_optional_libraries": [name for name in ("mutagen", "pypdf", "pypdfium2", "cryptography") if name in sys.modules],
            }), encoding="utf-8")
        if options.archive_check:
            from .archive_check import ArchiveCheck
            window._archive_check = ArchiveCheck(window, options.archive_check)
        elif options.cleanup_check:
            from .tracks_check import TracksCheck
            window._tracks_check = TracksCheck(window, options.cleanup_check, external=True, remux=True, cleanup=True)
        elif options.hevc_size_check:
            from .video_check import VideoCheck
            window._video_check = VideoCheck(window, options.hevc_size_check, compression=True, formats=True)
        elif options.remux_check:
            from .tracks_check import TracksCheck
            window._tracks_check = TracksCheck(window, options.remux_check, external=True, remux=True)
        elif options.subtitle_check:
            from .tracks_check import TracksCheck
            window._tracks_check = TracksCheck(window, options.subtitle_check, external=True)
        elif options.tracks_check:
            from .tracks_check import TracksCheck
            window._tracks_check = TracksCheck(window, options.tracks_check)
        elif options.webm_check:
            from .video_check import VideoCheck
            window._video_check = VideoCheck(window, options.webm_check, webm=True)
        elif options.format_check:
            from .video_check import VideoCheck
            window._video_check = VideoCheck(window, options.format_check, formats=True)
        elif options.compress_check:
            from .video_check import VideoCheck
            window._video_check = VideoCheck(window, options.compress_check, compression=True)
        elif options.video_check:
            from .video_check import VideoCheck
            window._video_check = VideoCheck(window, options.video_check)
        elif options.resize_check:
            from .image_check import ImageCheck
            window._image_check = ImageCheck(window, options.resize_check, resize=True)
        elif options.image_check:
            from .image_check import ImageCheck
            window._image_check = ImageCheck(window, options.image_check)
        elif options.undo_check:
            from .undo_check import UndoCheck
            window._undo_check = UndoCheck(window, options.undo_check)
        elif options.rename_check:
            from .rename_check import RenameCheck
            window._rename_check = RenameCheck(window, options.rename_check)
        elif options.queue_check:
            from .queue_check import QueueCheck
            window._queue_check = QueueCheck(window, options.queue_check)
        elif options.sample_rows:
            window.start_scan(sample_count=min(max(options.sample_rows, 0), 100000))
        elif options.screenshot:
            take_screenshot()
    def take_screenshot():
        if options.screenshot:
            options.screenshot.parent.mkdir(parents=True, exist_ok=True)
            if not window.grab().save(str(options.screenshot)):
                raise RuntimeError("Unable to save screenshot")
            QTimer.singleShot(0, window.close)
    window.painted.connect(ready)
    if options.screenshot and options.sample_rows:
        window.scan_complete.connect(take_screenshot)
    if options.exit_after_ms:
        QTimer.singleShot(options.exit_after_ms, window.close)
    window.show()
    return app.exec()
