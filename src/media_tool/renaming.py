"""Read-only rename planning and a journaled, same-folder rename adapter."""
from collections import deque
from dataclasses import replace
import fnmatch
import os
from pathlib import Path
import re
import stat
import sys
import uuid

from .domain import path_key
from .planning import OperationPlan, PlanItem, PlanCancelled, validate_name, _validate_request


def fingerprint(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def plain_file(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.is_symlink() or not info.st_ino:
        raise ValueError("Rename requires an ordinary file with a stable file identity.")
    return info


def plan_rename(records, request, cancelled=lambda: False):
    _validate_request(replace(request, prefix="") if request.rename_style == "clean" else request)
    if request.rename_style not in ("sequence", "random", "clean"):
        raise ValueError("Choose a supported naming style.")
    if request.separator not in (" ", "_", "-"):
        raise ValueError("Choose space, underscore or hyphen as the separator.")
    if request.rename_style == "random" and not request.seed:
        request = replace(request, seed=uuid.uuid4().hex)
    ordered = sorted(records, key=lambda record: record.sort_key)
    items, seen, identities, parents = [], set(), set(), {}
    sequence = request.start
    for record in ordered:
        if cancelled():
            raise PlanCancelled()
        parent_id = None
        try:
            key = record.path if record.sample else path_key(record.path)
            if key in seen:
                items.append(PlanItem(record, None, "Skipped", "Duplicate input path."))
                continue
            seen.add(key)
            if request.pattern and not fnmatch.fnmatchcase(record.name.casefold(), request.pattern.casefold()):
                items.append(PlanItem(record, None, "Skipped", "Filename does not match the pattern."))
                continue
            if not record.sample:
                info = plain_file(record.path)
                if fingerprint(info) != [record.device, record.inode, record.size, record.modified_ns]:
                    raise ValueError("Source changed since discovery. Remove it and add it again.")
                file_id = (info.st_dev, info.st_ino)
                if file_id in identities:
                    raise ValueError("Two selected paths share a file identity. Rename hard links separately.")
                identities.add(file_id)
                parent = Path(record.path).parent
                parent_key = path_key(parent)
                if parent_key not in parents:
                    parents[parent_key] = (parent.stat().st_dev, parent.stat().st_ino)
                parent_id = parents[parent_key]
                if not parent_id[1]:
                    raise ValueError("The folder does not provide a stable file identity.")
            source = Path(record.name)
            if request.rename_style == "sequence":
                name = f"{request.prefix}{sequence:0{request.padding}d}{source.suffix}"
                sequence += 1
            elif request.rename_style == "random":
                token = uuid.uuid5(uuid.NAMESPACE_URL, request.seed + "\0" + record.path).hex[:16]
                name = request.prefix + token + source.suffix
            else:
                stem = re.sub(r"[\s_.-]+", request.separator, source.stem).strip(" _.-")
                name = stem + source.suffix
            error = validate_name(name)
            if error or (request.rename_style == "clean" and not stem):
                raise ValueError(error or "Cleaning leaves an empty filename.")
            output = Path(name) if record.sample else Path(record.path).parent / name
            state = "Sample" if record.sample else ("Unchanged" if str(output) == record.path else "Planned")
            reason = {"Sample": "Illustrative sample only; no file exists.",
                      "Unchanged": "Already has this name.",
                      "Planned": "Rename in the same folder; extension and contents stay unchanged."}[state]
            items.append(PlanItem(record, str(output), state, reason, parent_id))
        except (OSError, ValueError, RuntimeError) as error:
            items.append(PlanItem(record, None, "Invalid", str(error)))

    # Existing names belonging to moving inputs can be vacated together. Dependency
    # propagation handles skipped inputs without repeated whole-manifest passes.
    moving = {path_key(item.source.path) for item in items if item.status == "Planned"}
    original_moving = moving.copy()
    reserved, suffixes, dependents = {}, {}, {}

    def assign(index):
        item = items[index]
        base = Path(item.output)
        target = base
        counter = suffixes.get(path_key(base), 1)
        while True:
            if cancelled():
                raise PlanCancelled()
            key = path_key(target)
            conflict = key in reserved or (os.path.lexists(target) and key not in moving)
            if not conflict:
                break
            if request.collision == "skip":
                items[index] = replace(item, status="Skipped", reason="Output is occupied by a file that will stay, or another planned output.")
                moving.discard(path_key(item.source.path))
                return
            target = base.with_name(f"{base.stem}_{counter}{base.suffix}")
            counter += 1
            error = validate_name(target.name)
            if error:
                items[index] = replace(item, output=None, status="Invalid", reason=error)
                moving.discard(path_key(item.source.path))
                return
        suffixes[path_key(base)] = counter
        reserved[key] = index
        if str(target) != item.output:
            item = replace(item, output=str(target), reason="A unique name avoids an occupied or already planned output.")
        items[index] = item
        dependents.setdefault(key, []).append(index)

    for index, item in enumerate(items):
        if item.status == "Planned":
            assign(index)
    blocked = deque(original_moving - moving)
    while blocked:
        key = blocked.popleft()
        for index in dependents.pop(key, ()):
            item = items[index]
            if item.status != "Planned" or path_key(item.output) != key:
                continue
            if reserved.get(key) == index:
                del reserved[key]
            before = path_key(item.source.path)
            assign(index)
            if before not in moving:
                blocked.append(before)
    return OperationPlan(request, tuple(items), (
        "Start Rename applies this preview. Completed batches can be undone from Results.",
    ))


def rename_snapshot(plan):
    if plan.request.operation != "rename":
        raise ValueError("Preview a rename operation first.")
    if any(item.status in ("Invalid", "Sample", "Incompatible") for item in plan.items):
        raise ValueError("Resolve invalid or sample entries before queuing this batch.")
    if not any(item.status == "Planned" for item in plan.items):
        raise ValueError("This preview has no names to change.")
    result = []
    for item in plan.items:
        record = item.source
        if item.status == "Planned" and not item.parent_identity:
            raise ValueError("Preview again to capture the folder identity.")
        result.append({"source": record.path, "output": item.output,
                       "identity": [record.device, record.inode, record.size, record.modified_ns],
                       "parent": str(Path(record.path).parent),
                       "parent_identity": item.parent_identity, "name": record.name,
                       "initial_state": "queued" if item.status == "Planned" else "skipped",
                       "initial_message": item.reason})
    validate_specs(result)
    return tuple(result)


def validate_specs(specs):
    sources, outputs = set(), set()
    for spec in specs:
        if spec.get("initial_state", "queued") != "queued":
            continue
        source, output, parent = Path(spec["source"]), Path(spec["output"]), Path(spec["parent"])
        if not source.is_absolute() or not output.is_absolute() or source.parent != parent or output.parent != parent:
            raise ValueError("Rename inputs and outputs must be absolute paths in the same folder.")
        if validate_name(output.name) or output.suffix != source.suffix:
            raise ValueError("Rename must use a valid filename and preserve its extension.")
        source_key, output_key = path_key(source), path_key(output)
        if source_key in sources or output_key in outputs:
            raise ValueError("Rename plans cannot repeat a source or final output name.")
        sources.add(source_key)
        outputs.add(output_key)


def move_no_replace(source, output):
    """Atomic same-filesystem move, with no overwrite and no link/unlink gap."""
    if os.name == "nt":
        os.rename(source, output)
    elif sys.platform.startswith("linux"):
        import ctypes
        library = ctypes.CDLL(None, use_errno=True)
        rename = getattr(library, "renameat2", None)
        if rename is None:
            raise OSError("Protected rename is unavailable on this system.")
        rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(source), -100, os.fsencode(output), 1) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), str(output))
    else:
        raise OSError("Rename execution is supported on Windows and Linux only.")
    if os.name != "nt":
        from .jobs import sync_directory
        sync_directory(str(Path(output).parent))


def owns(path, spec, exact=False):
    try:
        path = Path(path)
        if fingerprint(plain_file(path)) != spec["identity"]:
            return False
        if exact and os.name == "nt" and path.resolve(strict=True).name != path.name:
            return False
        return True
    except (OSError, ValueError):
        return False


def check_parent(spec):
    parent = Path(spec["parent"])
    if list(fingerprint(parent.stat())[:2]) != list(spec["parent_identity"]):
        raise ValueError("A source folder was replaced. Nothing else will be renamed.")
    # Source/output must remain in the same, originally selected folder.
    if Path(spec["source"]).parent != parent or Path(spec["output"]).parent != parent:
        raise ValueError("Rename cannot move a file to a different folder.")


def reconcile(store, item):
    import json
    spec = json.loads(item["spec"])
    check_parent(spec)
    phase = item["rename_phase"]
    if phase in ("committing", "committed") and owns(spec["output"], spec, exact=True):
        store.set_item(item["id"], "succeeded", success_message(spec), rename_phase="committed")
        return "committed"
    if phase in ("staging", "staged", "committing", "rolling_back") and owns(item["stage"], spec):
        return "staged"
    if phase in ("pending", "staging", "rolling_back") and owns(spec["source"], spec, exact=True):
        if os.path.lexists(item["stage"]):
            raise ValueError("The reserved staging name is occupied; it was left untouched.")
        return "pending"
    raise ValueError("A tracked file changed or is missing. Source, stage and output were left untouched.")


def success_message(spec):
    return ("Original name restored; file contents unchanged." if spec.get("undo_of_job") else
            "Renamed; file contents unchanged.")


def execute_rename_job(store, job_id, cancelled, paused, progress, hook=lambda point: None):
    import json
    records = store.items(job_id)
    active = [item for item in records if item["state"] not in ("succeeded", "skipped")]
    if not active:
        return store.summarize(job_id)
    if any(item["state"] != "queued" for item in active):
        for item in active:
            if item["state"] == "queued":
                store.set_item(item["id"], "interrupted", "Retry this interrupted rename batch before continuing.")
        return store.summarize(job_id)
    store.set_job(job_id, "running")
    has_committed = any(item["rename_phase"] == "committed" for item in records)
    positions = {}
    try:
        validate_specs([json.loads(item["spec"]) for item in records])
        # Reconciliation records a move that finished before its history update.
        for item in active:
            position = reconcile(store, item)
            positions[item["id"]] = position
            has_committed |= position == "committed"
        remaining = [item for item in active if positions[item["id"]] != "committed"]
        for item in remaining:
            spec = json.loads(item["spec"])
            if positions[item['id']] == 'pending':
                from .workflows import guard_workflow_inputs
                guard_workflow_inputs(store,spec['source'],spec['identity'],job_id)
            if not Path(spec["source"]).is_absolute() or validate_name(Path(spec["output"]).name):
                raise ValueError("Invalid source or output name in the saved plan.")
            check_parent(spec)
        pending_sources = {path_key(json.loads(item["spec"])["source"]): item for item in remaining
                           if positions[item["id"]] == "pending"}
        for item in remaining:
            spec = json.loads(item["spec"])
            target = spec["output"]
            if os.path.lexists(target):
                owner = pending_sources.get(path_key(target))
                if owner is None or not owns(target, json.loads(owner["spec"])):
                    raise FileExistsError(f"Output is now occupied: {target}")
            if positions[item["id"]] == "pending" and os.path.lexists(item["stage"]):
                raise FileExistsError(f"Reserved staging name is occupied: {item['stage']}")
        for item in remaining:
            store.set_item(item["id"], "running", "Preparing rename batch",
                           attempts=item["attempts"] + 1)
        for item in remaining:
            if (cancelled() or paused()) and not has_committed:
                raise InterruptedError("cancelled" if cancelled() else "paused")
            spec = json.loads(item["spec"])
            if positions[item["id"]] == "staged":
                continue
            check_parent(spec)
            if not owns(spec["source"], spec, exact=True):
                raise ValueError(f"Source changed before staging: {spec['source']}")
            store.set_item(item["id"], "running", "Staging original name", rename_phase="staging")
            hook("stage_intent")
            if not owns(spec["source"], spec, exact=True):
                raise ValueError("Source changed after staging was prepared.")
            move_no_replace(spec["source"], item["stage"])
            positions[item["id"]] = "staged"
            hook("staged")
            store.set_item(item["id"], "running", "Original safely staged", rename_phase="staged")
        if (cancelled() or paused()) and not has_committed:
            raise InterruptedError("cancelled" if cancelled() else "paused")
        hook("all_staged")
        # Final commits are a batch boundary: finishing a cycle is safer than
        # interrupting it while another original name is being reused.
        done = len([item for item in records if item["state"] in ("succeeded", "skipped")])
        for item in remaining:
            spec = json.loads(item["spec"])
            check_parent(spec)
            if not owns(item["stage"], spec):
                raise ValueError("A staged file changed; it was left untouched.")
            store.set_item(item["id"], "committing", "Applying final name", rename_phase="committing")
            hook("commit_intent")
            if not owns(item["stage"], spec):
                raise ValueError("Staged file changed after commit was prepared.")
            move_no_replace(item["stage"], spec["output"])
            positions[item["id"]] = "committed"
            has_committed = True
            hook("committed")
            if not owns(spec["output"], spec, exact=True):
                raise ValueError("Final file identity did not match the saved plan.")
            store.set_item(item["id"], "succeeded", success_message(spec), rename_phase="committed")
            done += 1
            progress(item["id"], done, len(records))
        return store.summarize(job_id)
    except Exception as error:
        # A filesystem operation can succeed immediately before its caller reports
        # an error (for example, a directory flush). Reconcile before rollback.
        for item in active:
            try:
                position = reconcile(store, store.item(item["id"]))
                positions[item["id"]] = position
                has_committed |= position == "committed"
            except (OSError, ValueError):
                pass
        stop = str(error) if isinstance(error, InterruptedError) else None
        # A pre-commit stop restores original names. After any final commit,
        # keep the remaining files staged so retry can finish without overwrites.
        restore = not has_committed
        restore_errors = {}
        if restore:
            for item in reversed(active):
                if positions.get(item["id"]) != "staged":
                    continue
                spec = json.loads(item["spec"])
                try:
                    check_parent(spec)
                    if not owns(item["stage"], spec):
                        raise ValueError("Staged file changed; it was not moved.")
                    store.set_item(item["id"], "running", "Restoring original name", rename_phase="rolling_back")
                    hook("rollback_intent")
                    move_no_replace(item["stage"], spec["source"])
                    positions[item["id"]] = "pending"
                    hook("rolled_back")
                    store.set_item(item["id"], "running", "Original name restored", rename_phase="pending")
                except Exception as rollback_error:
                    restore_errors[item["id"]] = str(rollback_error)
        for item in active:
            current = store.item(item["id"])
            if positions.get(item["id"]) == "committed" or current["state"] == "succeeded":
                # If final history failed, retain the commit intent for retry.
                if current["state"] != "succeeded":
                    store.set_item(item["id"], "interrupted", "Final move completed; retry verifies its identity.")
                continue
            held = positions.get(item["id"]) == "staged"
            state = ("queued" if stop == "paused" else "cancelled") if stop and not held else "failed"
            message = "Paused; original name restored." if stop == "paused" and not held else str(error)
            if held or item["id"] in restore_errors:
                message += f" File retained at {item['stage']}. Resolve the conflict and retry."
            if item["id"] in restore_errors:
                message += " Restore: " + restore_errors[item["id"]]
            store.set_item(item["id"], state, message)
        return store.summarize(job_id)
