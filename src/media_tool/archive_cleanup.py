"""Explicit archive source cleanup, independent of archive creation success."""
import json
import os
from pathlib import Path

from .domain import path_key
from .jobs import hash_file, owns_temp, identity, regular
from .original_cleanup import move_to_trash
from .zip_archive import check_entry, check_parent


def cleanup_archive(store, item_id, cancelled=lambda: False, hook=lambda point: None):
    row = store.item(item_id)
    spec = json.loads(row["spec"])
    if row["state"] != "succeeded" or not row["digest"] or not row["source_digest"]:
        raise ValueError("Only a fully verified, successfully saved archive can clean up sources.")
    digests = json.loads(row["source_digest"])
    ledger = json.loads(row["original_cleanup"]) if row["original_cleanup"] else {"files": {}}
    files = ledger.setdefault("files", {})

    def save():
        moved = sum(value["state"] == "moved" for value in files.values())
        kept = sum(value["state"] != "moved" for value in files.values())
        ledger.update(state="partial" if any(v["state"] == "pending" for v in files.values()) else "complete", moved=moved, kept=kept)
        current = store.item(item_id)
        creation = current["message"].split(" Source cleanup:", 1)[0].replace(" Originals kept.", "")
        store.set_item(item_id, current["state"], creation + f" Source cleanup: {moved} moved; {kept} kept.", original_cleanup=json.dumps(ledger))

    output_identity = None
    def verified_output(full=False):
        nonlocal output_identity
        check_parent(spec)
        current_identity = identity(regular(spec["output"]))
        if (not owns_temp(store.item(item_id), Path(spec["output"])) or
                (output_identity is not None and current_identity != output_identity) or
                (full and hash_file(spec["output"], cancelled) != row["digest"])):
            raise ValueError("Verified archive changed or is no longer owned; originals kept.")
        output_identity = current_identity

    if cancelled():
        for entry in spec["manifest"]["entries"]:
            if not entry["folder"] and files.get(entry["source"], {}).get("state") != "moved":
                files[entry["source"]] = dict(state="kept", message="Cleanup cancelled; original kept.")
        save()
        return ledger
    verified_output(full=True)
    for entry in spec["manifest"]["entries"]:
        if entry["folder"]:
            continue
        source = entry["source"]
        if files.get(source, {}).get("state") == "moved":
            continue
        if cancelled():
            files[source] = dict(state="kept", message="Cleanup cancelled; original kept.")
            save()
            continue
        try:
            if not os.path.lexists(source):
                raise ValueError("Original is absent; check the Recycle Bin if cleanup was interrupted")
            check_entry(entry, spec)
            if source not in digests or hash_file(source, cancelled) != digests[source]:
                raise ValueError("Original bytes changed after archive verification")
            from .workflows import guard_workflow_inputs
            guard_workflow_inputs(store,source,entry['identity'],row['job_id'])
            for other in store.db.execute("SELECT id,spec FROM items WHERE id<>? AND state IN ('queued','running','verifying','committing')", (item_id,)):
                other_spec = json.loads(other["spec"])
                dependencies = [(other_spec.get("source"), other_spec.get("identity", []))]
                dependencies.extend((e["source"], e["identity"]) for e in other_spec.get("manifest", {}).get("entries", []) if not e["folder"])
                if any(path and (path_key(path) == path_key(source) or frozen[:2] == entry["identity"][:2]) for path, frozen in dependencies):
                    raise ValueError("Another pending job needs this source")
            files[source] = dict(state="pending", message="Moving verified source to Recycle Bin / Trash")
            save()
            hook("archive_cleanup_prepared")
            verified_output()
            check_entry(entry, spec)
            if hash_file(source, cancelled) != digests[source]:
                raise ValueError("Original changed before cleanup")
            check_entry(entry, spec)
            if cancelled():
                raise ValueError("Cleanup cancelled")
            trash_path = move_to_trash(Path(source))
            files[source] = dict(state="moved", message="Moved to Recycle Bin / Trash", trash_path=trash_path)
            save()
            hook("archive_source_trashed")
        except Exception as error:
            if files.get(source, {}).get("state") != "moved":
                files[source] = dict(state="kept", message=str(error))
            save()
    save()
    return ledger
