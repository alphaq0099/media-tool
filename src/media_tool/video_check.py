"""Opt-in native workspace trial with disposable generated video fixtures."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import time

from PySide6.QtCore import QTimer, Qt
from .jobs_ui import QueueDialog
from .tool_paths import resolve_tool


class VideoCheck:
    def __init__(self, window, report, compression=False, formats=False, webm=False):
        self.window, self.report = window, Path(report).resolve()
        self.compression = compression
        self.formats = formats or webm
        self.webm = webm
        self.report.parent.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="mediatool-video-")
        self.root = Path(self.temporary.name)
        self.output = self.root / "converted"
        self.output.mkdir()
        self.before = {}
        self.phase, self.shots = 0, {}
        self.started = self.last = time.perf_counter()
        self.gaps = []
        ffmpeg = resolve_tool("ffmpeg")
        for number, (extension, audio) in enumerate((("mp4", 1), ("mkv", 0), ("mov", 2), ("webm", 1)), 1):
            path = self.root / f"sample {number} 日本.{extension}"
            args = [ffmpeg, "-hide_banner", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=320x180:rate=60" if number == 1 else
                    "testsrc2=size=180x320:rate=12" if number == 2 else "testsrc2=size=320x180:rate=12"]
            for track in range(audio):
                args += ["-f", "lavfi", "-i", f"sine=frequency={440*(track+1)}:sample_rate=48000"]
            args += ["-map", "0:v:0"]
            for track in range(audio):
                args += ["-map", f"{track+1}:a:0"]
            if number == 1:
                args += ["-vf", "select='if(lt(t,0.5),1,not(mod(n,3)))'", "-fps_mode:v", "passthrough",
                         "-enc_time_base:v", "1:30000", "-bsf:v", "h264_metadata=tick_rate=60:fixed_frame_rate_flag=0"]
            elif number == 3:
                args += ["-vf", "setsar=4/3"]
            args += ["-t", "1.5", "-c:v", "libvpx-vp9" if extension == "webm" else "libx264", "-threads", "1", "-pix_fmt", "yuv420p"]
            if extension != "webm":
                args += ["-preset", "ultrafast"]
            if audio:
                args += ["-c:a", "libopus" if extension == "webm" else "aac"]
            subprocess.run(args + [str(path)], check=True, capture_output=True, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if number == 3:
                rotated = self.root / "rotated.mov"
                subprocess.run([ffmpeg, "-hide_banner", "-v", "error", "-display_rotation:v:0", "90",
                    "-i", str(path), "-map", "0", "-c", "copy", str(rotated)], check=True, capture_output=True, timeout=30,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                rotated.replace(path)
            self.before[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.queue = QueueDialog(window, self.root / "history")
        window.jobs_dialog = self.queue
        self.queue.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.waiting = self.queue.store.enqueue("Waiting", "sample", [{"name": "later", "seconds": 0}])
        window._tool_changed("Video")
        if webm:
            window.format.setCurrentText("WebM")
        if formats:
            window.format.setCurrentText("MKV")
            codec = window.video_format_controls.codec
            codec.setCurrentIndex(codec.findData("hevc"))
        assert window.plan_request().video_crf == 23
        assert window.plan_request().video_preset == "veryfast"
        window.detail.setCurrentIndex(window.detail.findData(20))
        window.video_speed.setCurrentIndex(window.video_speed.findData("superfast"))
        resize = window.resize_controls
        resize.mode.setCurrentIndex(resize.mode.findData("dimensions"))
        resize.width.setValue(160)
        resize.height.setValue(120)
        if compression:
            controls = window.compression_controls
            controls.mode.setCurrentIndex(controls.mode.findData("target_size"))
            controls.amount.setValue(0.10)
            controls.unit.setCurrentText("MiB")
            controls.audio.setCurrentIndex(controls.audio.findData(96))
            controls.reuse.setChecked(formats)
        window.destination.setText(str(self.output))
        window.video_audio_mode.setCurrentIndex(window.video_audio_mode.findData("none"))
        self.timer = QTimer(window)
        self.timer.setInterval(10)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        window.start_scan(list(self.before))

    def screenshots(self, stage):
        if self.formats:
            self.window.settings_scroll.verticalScrollBar().setValue(0)
        if self.compression:
            self.window.settings_scroll.ensureWidgetVisible(self.window.compression_controls.target_panel, 10, 10)
        for theme in ("light", "dark"):
            self.window.set_theme(theme, persist=False)
            path = self.report.with_name(f"{self.report.stem}-{stage}-{theme}.png")
            assert self.window.grab().save(str(path))
            self.shots[f"{stage}_{theme}"] = str(path)

    def tick(self):
        current = time.perf_counter()
        if self.phase == 3:
            self.gaps.append((current - self.last) * 1000)
        self.last = current
        try:
            if current - self.started > 100:
                raise TimeoutError("Video diagnostic timed out")
            window, queue = self.window, self.queue
            if self.phase == 0 and not window.busy and window.start_rename_button.isEnabled():
                if window.resize_controls.preview.text() != "First of 4 files\n320 × 180 → 160 × 90":
                    return
                window.start_rename_button.click()
                assert window.plan_worker is not None or window.last_plan is not None, window.status.text()
                self.phase = 1
            elif self.phase == 1 and not window.busy:
                assert window.last_plan.counts == {"Planned": 1, "Review": 3}, window.status.text()
                assert not queue.store.jobs()[1:]
                assert not list(self.output.iterdir())
                self.screenshots("review")
                window.video_omissions.setChecked(True)
                window.video_audio_mode.setCurrentIndex(window.video_audio_mode.findData("all"))
                window.preview_changes()
                self.phase = 2
            elif self.phase == 2 and not window.busy:
                assert window.last_plan.counts == {"Planned": 4}, window.status.text()
                self.dimensions = [item.video_spec["output_size"] for item in window.last_plan.items]
                assert self.dimensions == [[160, 90], [66, 120], [50, 120], [160, 90]], self.dimensions
                assert window.start_rename_button.text() == ("Start Compress" if self.compression else "Start Resize")
                if self.compression:
                    assert all(item.video_spec["budget"]["target_bytes"] == 104_857 for item in window.last_plan.items)
                assert window.stream_details_button.isVisible()
                self.screenshots("preview")
                self.conversion_started = time.perf_counter()
                window.start_rename_button.click()
                self.phase = 3
                self.last = time.perf_counter()
            elif self.phase == 3 and not window.busy and not queue.enabled:
                seconds = time.perf_counter() - self.conversion_started
                assert window.result_model.rowCount() == 4
                assert all(item["state"] == "succeeded" for item in window.result_model.items), window.status.text()
                assert all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest for path, digest in self.before.items())
                assert len(list(self.output.iterdir())) == 4
                if self.formats:
                    assert all(path.suffix == (".webm" if self.webm else ".mkv") for path in self.output.iterdir())
                    assert all(("VP9" if self.webm else "H.265 (HEVC)") in item["details"] for item in window.result_model.items)
                if self.compression:
                    assert all(path.stat().st_size <= 104_857 for path in self.output.iterdir())
                    assert all("Target met:" in item["details"] for item in window.result_model.items)
                    assert not list((self.root / "history").glob(".mediatool-pass-*"))
                assert queue.store.items(self.waiting)[0]["attempts"] == 0
                assert not queue.isVisible()
                assert not window.result_can_undo
                assert window.model.rowCount() == window.plan_model.rowCount() == 0
                window.results_table.selectRow(0)
                self.screenshots("results")
                self.finish(dict(passed=True, files=4, omitted_track_review_blocked=True,
                    vfr_fixture=True, live_dimensions_verified=True,
                    target_size_verified=self.compression, target_bytes=104_857 if self.compression else None,
                    reusable_analysis_enabled=window.compression_controls.reuse.isChecked(),
                    measured_sizes=[path.stat().st_size for path in sorted(self.output.iterdir())],
                    rotated_non_square_pixel_fixture=True, output_dimensions=self.dimensions,
                    compression_mode=window.compression_controls.mode.currentData(),
                    video_codec=window.video_format_controls.codec.currentData(), container=window.format.currentText(),
                    quality=None if self.compression else window.detail.currentText(), speed=window.video_speed.currentText(),
                    original_hashes_unchanged=True, output_streams_duration_and_decode_verified=True,
                    waiting_job_untouched=True, results_in_workspace=True, conversion_seconds=seconds,
                    execution_heartbeat_max_ms=max(self.gaps, default=0), screenshots=self.shots))
        except Exception as error:
            self.finish(dict(passed=False, phase=self.phase, error=str(error) or type(error).__name__,
                status=self.window.status.text(), files=self.window.model.rowCount(), visible=self.window.proxy.rowCount(),
                start_enabled=self.window.start_rename_button.isEnabled(), scan_issues=self.window.issues,
                dimension_preview=self.window.resize_controls.preview.text()))

    def finish(self, result):
        self.timer.stop()
        self.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        self.window.close()
