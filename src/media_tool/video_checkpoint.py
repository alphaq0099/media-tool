"""Completed first-pass checkpoints; source, encoder, settings and logs must match."""
import hashlib
import json
import os
from pathlib import Path

from .jobs import hash_file, checkpoint, identity, regular
from .video_stats import check_scope, cleanup_scope


def fingerprint(spec, source_digest, engine_digest, bitrate, attempt):
    data = dict(version=1, spec=spec, source=source_digest, encoder=engine_digest, bitrate=bitrate, attempt=attempt)
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def save_analysis(store, item_id, scope, spec, source_digest, engine_digest, bitrate, attempt, cancelled):
    root = check_scope(store, scope)
    logs = {}
    for path in root.iterdir():
        checkpoint(cancelled)
        if path.name.endswith(".temp") or not path.stat().st_size:
            continue
        with path.open("rb+") as handle:
            os.fsync(handle.fileno())
        logs[path.name] = dict(identity=identity(regular(path)), digest=hash_file(path, cancelled))
    main = "x265.log" if spec.get("codec") == "hevc" else "stats-0.log"
    if main not in logs:
        raise ValueError("Completed analysis did not create a reusable statistics log.")
    scope["analysis"] = dict(attempt=attempt, bitrate=bitrate, logs=logs,
        fingerprint=fingerprint(spec, source_digest, engine_digest, bitrate, attempt))
    row = store.item(item_id)
    store.set_item(item_id, row["state"], "Completed analysis saved for retry", stage=json.dumps(scope))


def cached_analysis(store, item_id, spec, source_digest, engine_digest, cancelled):
    row = store.item(item_id)
    if not row["stage"]:
        return None
    scope = json.loads(row["stage"])
    if not os.path.lexists(scope["path"]):
        cleanup_scope(store, item_id)
        return None
    root = check_scope(store, scope)  # Foreign/replaced files are never deleted.
    analysis = scope.get("analysis", {})
    bitrate, attempt = analysis.get("bitrate", 0), analysis.get("attempt", -1)
    valid = (attempt in (0, 1) and 32_000 <= bitrate <= spec["budget"]["video_bitrate"] and
             scope.get("codec", "h264") == spec.get("codec", "h264") and
             analysis.get("fingerprint") == fingerprint(spec, source_digest, engine_digest, bitrate, attempt))
    logs = analysis.get("logs", {})
    valid = valid and ("x265.log" if spec.get("codec") == "hevc" else "stats-0.log") in logs
    if valid:
        for name, saved in logs.items():
            checkpoint(cancelled)
            path = root / name
            # Membership was checked by check_scope; reject malformed journal names.
            if Path(name).name != name or not path.exists() or identity(regular(path)) != saved["identity"] or hash_file(path, cancelled) != saved["digest"]:
                valid = False
                break
    if valid:
        return scope
    cleanup_scope(store, item_id)
    store.set_item(item_id, row["state"], "Saved analysis no longer matches; restarting both passes")
    return None
