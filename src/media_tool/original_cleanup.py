"""User-selected source-to-trash cleanup after a verified, durable media commit."""
import json
import os
from pathlib import Path

from .jobs import check_source, hash_file, owns_temp


def move_to_trash(path):
    from PySide6.QtCore import QFile
    if not QFile.supportsMoveToTrash():
        raise OSError("Recycle Bin / Trash is unavailable on this system.")
    file = QFile(str(path))
    if not file.moveToTrash():
        raise OSError(file.errorString() or "The original could not be moved to the Recycle Bin.")
    return file.fileName()


def cleanup_original(store, item_id, cancelled, hook=lambda point: None):
    row = store.item(item_id)
    spec = json.loads(row["spec"])
    prior = json.loads(row["original_cleanup"]) if row["original_cleanup"] else {}
    if prior.get("state") == "moved":
        return "Original moved to Recycle Bin / Trash."

    def record(state, message, **data):
        current = store.item(item_id)
        store.set_item(item_id, current["state"], current["message"],
                       original_cleanup=json.dumps(dict(state=state, message=message, **data)))
        return message

    try:
        if cancelled():
            return record("kept", "Original kept: cleanup cancelled after the output was saved.")
        if not row["digest"] or not row["source_digest"] or not owns_temp(row, Path(spec["output"])):
            raise ValueError("committed-output ownership or source checksum is unavailable")
        if hash_file(spec["output"], cancelled) != row["digest"]:
            raise ValueError("verified output changed")
        if not os.path.lexists(spec["source"]):
            return record("absent", "Verified output saved; original is absent. If cleanup was interrupted, check the Recycle Bin.")
        check_source(spec)
        if os.path.samefile(spec["source"], spec["output"]):
            raise ValueError("source and output refer to the same file")
        if hash_file(spec["source"], cancelled) != row["source_digest"]:
            raise ValueError("original contents changed after verification")
        from .workflows import guard_workflow_inputs
        guard_workflow_inputs(store,spec['source'],spec['identity'],row['job_id'])
        for other in store.db.execute("SELECT id,spec FROM items WHERE id<>? AND state IN ('queued','running','verifying','committing')", (item_id,)):
            other_spec = json.loads(other["spec"])
            sources = [other_spec.get("source")]
            archive_entries = [entry for entry in other_spec.get("manifest", {}).get("entries", []) if not entry["folder"]]
            sources.extend(entry["source"] for entry in archive_entries)
            if (any(entry["identity"][:2] == spec["identity"][:2] for entry in archive_entries) or
                    any(source and os.path.normcase(os.path.abspath(source)) == os.path.normcase(os.path.abspath(spec["source"])) for source in sources)):
                raise ValueError("another pending job still needs the original")
        check_source(spec)
        if cancelled():
            return record("kept", "Original kept: cleanup cancelled after the output was saved.")
        record("pending", "Verified output saved; moving original to Recycle Bin.")
        hook("original_cleanup_prepared")
        check_source(spec)
        if hash_file(spec["output"], cancelled) != row["digest"]:
            raise ValueError("verified output changed before cleanup")
        if not owns_temp(store.item(item_id), Path(spec["output"])):
            raise ValueError("verified output ownership changed before cleanup")
        if hash_file(spec["source"], cancelled) != row["source_digest"]:
            raise ValueError("original contents changed before cleanup")
        check_source(spec)
        if cancelled():
            return record("kept", "Original kept: cleanup cancelled after the output was saved.")
        destination = move_to_trash(Path(spec["source"]))
        hook("original_trashed")
        return record("moved", "Original moved to Recycle Bin / Trash.", trash_path=destination)
    except Exception as error:
        # Output success is independent of optional cleanup. No permanent-delete
        # fallback and no failed-conversion retry solely because trash failed.
        return record("kept", f"Verified output saved; original cleanup skipped: {error}. Original kept if still present.")
