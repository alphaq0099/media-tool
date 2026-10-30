import json
from pathlib import Path
import os

import pytest

from media_tool.jobs import Store
from media_tool.video_checkpoint import cached_analysis, save_analysis
from media_tool.video_stats import create_scope, cleanup_scope
from test_foundation import app, window


@pytest.mark.parametrize("change", ["source", "encoder", "settings", "missing_log", "edited_log", "missing_directory", "incomplete"])
def test_stale_or_incomplete_analysis_restarts_and_cleans_owned_files(tmp_path, change):
    store = Store(tmp_path / "history")
    spec = dict(codec="hevc", budget=dict(video_bitrate=200000), preset="veryfast")
    try:
        job = store.enqueue("Checkpoint", "sample", [spec])
        item = store.items(job)[0]
        scope = create_scope(store, item["id"], "hevc")
        root = Path(scope["path"])
        log = root / "x265.log"
        log.write_bytes(b"completed analysis")
        if change != "incomplete":
            save_analysis(store, item["id"], scope, spec, "source-hash", "encoder-hash", 200000, 0, lambda: False)
        source, encoder = "source-hash", "encoder-hash"
        if change == "source":
            source = "different-source"
        elif change == "encoder":
            encoder = "different-encoder"
        elif change == "settings":
            spec["preset"] = "medium"
        elif change == "missing_log":
            log.unlink()
        elif change == "edited_log":
            info = log.stat()
            log.write_bytes(b"different analysis")  # Same length, identity and mtime.
            os.utime(log, ns=(info.st_atime_ns, info.st_mtime_ns))
        elif change == "missing_directory":
            for path in root.iterdir():
                path.unlink()
            root.rmdir()
        assert cached_analysis(store, item["id"], spec, source, encoder, lambda: False) is None
        assert not root.exists() and not store.item(item["id"])["stage"]
    finally:
        store.close()


def test_replaced_hevc_statistics_are_preserved(tmp_path):
    store = Store(tmp_path / "history")
    spec = dict(codec="hevc", budget=dict(video_bitrate=200000))
    try:
        job = store.enqueue("Checkpoint", "sample", [spec])
        item = store.items(job)[0]
        scope = create_scope(store, item["id"], "hevc")
        log = Path(scope["path"]) / "x265.log"
        log.write_bytes(b"analysis")
        save_analysis(store, item["id"], scope, spec, "source", "encoder", 200000, 0, lambda: False)
        log.rename(tmp_path / "owned-log")
        log.write_bytes(b"unrelated replacement")
        with pytest.raises(ValueError, match="replaced"):
            cached_analysis(store, item["id"], spec, "source", "encoder", lambda: False)
        assert log.read_bytes() == b"unrelated replacement"
        log.unlink()
        cleanup_scope(store, item["id"])
    finally:
        store.close()


def test_queue_discards_only_owned_analysis_while_paused(window, tmp_path):
    from media_tool.jobs_ui import QueueDialog
    queue = QueueDialog(window, tmp_path / "history")
    window.jobs_dialog = queue
    source = tmp_path / "source.mp4"
    output = tmp_path / "output.mp4"
    source.write_bytes(b"original")
    output.write_bytes(b"verified output")
    job = queue.store.enqueue("Target", "video", [dict(name="clip", source=str(source), output=str(output))])
    item = queue.store.items(job)[0]
    scope = create_scope(queue.store, item["id"], "hevc")
    root = Path(scope["path"])
    foreign = root / "keep.txt"
    foreign.write_text("user file")
    queue.refresh(job)
    assert queue.discard_analysis_button.isEnabled()
    queue.enabled = True
    queue.discard_analysis()
    assert root.exists() and queue.store.item(item["id"])["stage"]
    queue.enabled = False
    queue.discard_analysis_button.click()
    assert foreign.read_text() == "user file" and "unrelated" in queue.status.text()
    foreign.unlink()
    queue.discard_analysis_button.click()
    assert not root.exists() and not queue.store.item(item["id"])["stage"]
    assert source.read_bytes() == b"original" and output.read_bytes() == b"verified output"
    assert not queue.discard_analysis_button.isEnabled()
