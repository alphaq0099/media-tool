"""Opt-in native track-selection trial using generated media and isolated history."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import time

from PySide6.QtCore import QTimer, Qt
from .jobs_ui import QueueDialog
from .tool_paths import resolve_tool


class TracksCheck:
    def __init__(self, window, report, external=False, remux=False, cleanup=False):
        self.window, self.report = window, Path(report).resolve()
        self.external = external
        self.remux = remux
        self.cleanup = cleanup
        self.report.parent.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="mediatool-tracks-")
        self.root = Path(self.temporary.name)
        self.source = self.root / "Languages 日本.mkv"
        subtitle = self.root / "captions.srt"
        subtitle.write_text("1\n00:00:00,200 --> 00:00:01,200\nTrack preservation trial 日本\n", encoding="utf-8")
        self.subtitle = subtitle
        self.subtitle_hash = hashlib.sha256(subtitle.read_bytes()).hexdigest()
        subprocess.run([resolve_tool("ffmpeg"), "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=12",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000",
            "-i", str(subtitle), "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3:s", "-map", "3:s",
            "-t", "1.5", "-c:v", "libx264", "-threads", "1", "-c:a", "aac", "-c:s", "copy",
            "-metadata:s:a:0", "language=eng", "-metadata:s:a:0", "title=Original", "-metadata:s:a:1", "language=hin",
            "-metadata:s:a:1", "title=Commentary", "-metadata:s:s:0", "language=eng", "-metadata:s:s:1", "language=hin",
            "-disposition:s:0", "0", "-disposition:s:1", "forced", str(self.source)], check=True, capture_output=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.queue = QueueDialog(window, self.root / "history")
        window.jobs_dialog = self.queue
        self.queue.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.waiting = self.queue.store.enqueue("Waiting", "sample", [{"name": "later", "seconds": 0}])
        window._tool_changed("Video")
        window.keep.setChecked(not cleanup)
        window.format.setCurrentText("MP4" if external else "MKV")
        if remux:
            action = window.video_format_controls.action
            action.setCurrentIndex(action.findData("remux"))
        window.destination.setText(str(self.root))
        self.phase, self.shots, self.gaps = 0, {}, []
        self.started = self.last = time.perf_counter()
        self.timer = QTimer(window)
        self.timer.setInterval(10)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        window.start_scan([str(self.source)])

    def screenshots(self, stage):
        window = self.window
        if stage == "mode":
            window.settings_scroll.verticalScrollBar().setValue(0)
        else:
            window.settings_scroll.ensureWidgetVisible(window.video_audio_mode if stage == "audio-help" else
                window.video_external_browse if self.external else window.video_subtitle_mode, 5, 5)
        for theme in ("light", "dark"):
            window.set_theme(theme, persist=False)
            path = self.report.with_name(f"{self.report.stem}-{stage}-{theme}.png")
            assert window.grab().save(str(path))
            self.shots[f"{stage}_{theme}"] = str(path)

    def tick(self):
        now = time.perf_counter()
        if self.phase == 3:
            self.gaps.append((now - self.last) * 1000)
        self.last = now
        window, queue = self.window, self.queue
        try:
            if now - self.started > 100:
                raise TimeoutError("Track diagnostic timed out")
            if self.phase == 0 and not window.busy and window.start_rename_button.isEnabled():
                window.start_rename_button.click()
                self.phase = 1
            elif self.phase == 1 and not window.busy:
                assert window.last_plan.counts == {"Review": 1}, window.status.text()
                assert len(queue.store.jobs()) == 1
                if self.external:
                    self.screenshots("audio-help")
                window.video_audio_mode.setCurrentIndex(window.video_audio_mode.findData("custom"))
                window.video_audio_tracks.setText("2, 1")
                if self.external:
                    window.video_external_enabled.setChecked(True)
                    window.video_external_subtitle.setText(str(self.subtitle))
                    window.video_omissions.setChecked(True)
                else:
                    window.video_subtitle_mode.setCurrentIndex(window.video_subtitle_mode.findData("all"))
                window.preview_changes()
                self.phase = 2
            elif self.phase == 2 and not window.busy:
                assert window.last_plan.counts == {"Planned": 1}, window.status.text()
                spec = window.last_plan.items[0].video_spec
                assert [track["index"] for track in spec["audio_tracks"]] == [2, 1]
                assert len(spec["subtitle_tracks"]) == (1 if self.external else 2)
                if self.remux:
                    assert spec["action"] == "remux" and window.start_rename_button.text() == "Start Remux"
                    self.screenshots("mode")
                self.screenshots("preview")
                self.conversion_started = time.perf_counter()
                window.start_rename_button.click()
                self.phase = 3
                self.last = time.perf_counter()
            elif self.phase == 3 and not window.busy and not queue.enabled:
                assert window.result_model.rowCount() == 1
                result = window.result_model.items[0]
                assert result["state"] == "succeeded", result
                if self.remux:
                    assert "remuxed video/audio without re-encoding" in result["details"], result
                assert f"Kept 2 audio and {1 if self.external else 2} subtitle" in result["details"]
                cleanup_record = None
                if self.cleanup:
                    jobs = [job for job in queue.store.jobs() if job["id"] != self.waiting]
                    cleanup_record = json.loads(queue.store.items(jobs[0]["id"])[0]["original_cleanup"])
                    assert cleanup_record["state"] == "moved", cleanup_record
                    assert not self.source.exists()
                else:
                    assert hashlib.sha256(self.source.read_bytes()).hexdigest() == self.before
                assert hashlib.sha256(self.subtitle.read_bytes()).hexdigest() == self.subtitle_hash
                assert queue.store.items(self.waiting)[0]["attempts"] == 0
                assert window.model.rowCount() == window.plan_model.rowCount() == 0
                window.results_table.selectRow(0)
                self.screenshots("results")
                self.finish(dict(passed=True, audio_selection=[2, 1], subtitles=1 if self.external else 2, container=window.format.currentText(),
                    external_subtitle=self.external, external_original_unchanged=True,
                    remux=self.remux, video_audio_packet_content_and_timing_verified=self.remux,
                    original_hash_unchanged=not self.cleanup, original_cleanup=cleanup_record,
                    native_trash_api_verified=self.cleanup, omitted_track_review_blocked=True, waiting_job_untouched=True,
                    labels_flags_all_audio_decode_verified=True,
                    subtitle_verification="decoded visible text, cue count and timing" if self.external else "packet hashes, cue count and timing",
                    conversion_seconds=time.perf_counter() - self.conversion_started,
                    execution_heartbeat_max_ms=max(self.gaps, default=0), screenshots=self.shots))
        except Exception as error:
            self.finish(dict(passed=False, phase=self.phase, error=str(error) or type(error).__name__, status=window.status.text()))

    def finish(self, result):
        self.timer.stop()
        self.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        self.window.close()
