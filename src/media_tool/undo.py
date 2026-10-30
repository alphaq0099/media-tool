"""Read-only inverse plans derived from completed rename journals."""
from dataclasses import dataclass
import json
import os
from pathlib import Path

from .domain import FileRecord, category, path_key
from .planning import PlanCancelled, PlanItem
from .renaming import check_parent, owns, validate_specs


@dataclass(frozen=True, slots=True)
class UndoPlan:
    original_job: str
    specs: tuple
    items: tuple

    @property
    def can_apply(self):
        return bool(self.items) and all(item.status == "Ready" for item in self.items)


def inverse_specs(store, job_id):
    job = store.db.execute("SELECT adapter FROM jobs WHERE id=?", (job_id,)).fetchone()
    if job is None or job["adapter"] != "rename":
        raise ValueError("Select a completed rename job to undo.")
    if store.original_job(job_id):
        raise ValueError("This is already an undo job. Redo is not available.")
    if store.undo_job(job_id):
        raise ValueError("An undo already exists for this batch. Review or retry that undo in Queue.")
    items = store.items(job_id)
    if not items or any(item["state"] not in ("succeeded", "skipped") for item in items):
        raise ValueError("Finish or retry the original rename batch before undoing it.")
    specs = []
    for item in items:
        if item["state"] != "succeeded":
            continue
        if item["rename_phase"] != "committed":
            raise ValueError("The saved rename is not confirmed. Recover it before undoing.")
        original = json.loads(item["spec"])
        specs.append(dict(source=original["output"], output=original["source"],
                          identity=original["identity"], parent=original["parent"],
                          parent_identity=original["parent_identity"],
                          name=Path(original["output"]).name, initial_state="queued",
                          initial_message="Restore the exact original name.",
                          undo_of_job=job_id, undo_of_item=item["id"]))
    if not specs:
        raise ValueError("This batch did not rename any files.")
    validate_specs(specs)
    return tuple(specs)


def plan_undo(store, job_id, cancelled=lambda: False):
    specs = inverse_specs(store, job_id)
    moving = {path_key(spec["source"]): spec for spec in specs}
    items = []
    for spec in specs:
        if cancelled():
            raise PlanCancelled()
        state, reason = "Ready", "Restore the original name; file contents stay unchanged."
        try:
            check_parent(spec)
            if not owns(spec["source"], spec, exact=True):
                raise ValueError("Renamed file was edited, moved, replaced or is missing. It will not be changed.")
            if os.path.lexists(spec["output"]):
                owner = moving.get(path_key(spec["output"]))
                if owner is None or not owns(spec["output"], owner):
                    raise ValueError("Original name is occupied. Move the conflicting file before previewing again.")
        except (OSError, ValueError) as error:
            state, reason = "Conflict", str(error)
        device, inode, size, modified = spec["identity"]
        record = FileRecord(spec["source"], spec["name"], size, category(Path(spec["source"])),
                            False, modified, device, inode)
        items.append(PlanItem(record, spec["output"], state, reason, tuple(spec["parent_identity"])))
    return UndoPlan(job_id, specs, tuple(items))
