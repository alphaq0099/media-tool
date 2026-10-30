"""One-level folder flattening with no-replace moves and a durable undo journal."""
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path, PurePosixPath
import stat

from .domain import natural_key, path_key
from .folders import (FolderNode, aggregate, directory_identity, entry_identity,
                      linked, scan_folder, subtree_manifest)
from .jobs import Cancelled, checkpoint, sync_directory
from .planning import validate_name
from .renaming import move_no_replace


@dataclass(frozen=True, slots=True)
class FolderMove:
    source: str
    output: str | None
    status: str
    reason: str
    spec: dict | None = None


@dataclass(frozen=True, slots=True)
class FlattenPlan:
    root: str
    items: tuple
    original_job: str | None = None

    @property
    def can_apply(self):
        return any(i.status == "Ready" for i in self.items) and all(
            i.status in ("Ready", "Skipped") for i in self.items)

    @property
    def specs(self):
        if not self.can_apply:
            raise ValueError("Resolve conflicts and preview a nonempty folder move first.")
        return tuple(i.spec for i in self.items if i.spec is not None)


def plan_flatten(snapshot, include_files=False, collision="skip", remove_empty=True,
                 selected=None, cancelled=lambda: False):
    if not snapshot.complete:
        raise ValueError("Refresh and finish a complete folder scan before flattening.")
    if collision not in ("skip", "unique"):
        raise ValueError("Choose Skip or Generate unique name.")
    root, nodes = Path(snapshot.root), snapshot.nodes
    if directory_identity(root) != list(nodes[0].identity):
        raise ValueError("The root folder was replaced. Refresh before previewing.")
    parents = [i for i in snapshot.children[0] if nodes[i].kind == "Folder"]
    if selected is not None:
        chosen = set(selected)
        if not chosen or not chosen <= {nodes[i].path for i in parents}:
            raise ValueError("Select one or more immediate folders directly inside the root.")
        parents = [i for i in parents if nodes[i].path in chosen]
    parents.sort(key=lambda i: (natural_key(nodes[i].name), nodes[i].name))
    # All root entries reserve their names, even entries hidden by inspector filters.
    reserved = {path_key(n.path) for n in (nodes[i] for i in snapshot.children[0])}
    items = []
    for parent_index in parents:
        parent = nodes[parent_index]
        def skip(node, reason, output=None):
            spec = dict(source=node.path, output=str(output) if output else None,
                        name=node.name, kind=node.kind, root=str(root),
                        root_identity=list(nodes[0].identity), parent=str(root),
                        parent_identity=list(nodes[0].identity), source_parent=parent.path,
                        source_parent_identity=list(parent.identity), remove_empty=False,
                        initial_state="skipped", initial_message=reason)
            return FolderMove(node.path, spec["output"], "Skipped", reason, spec)
        for child in sorted(snapshot.children[parent_index], key=lambda i: (natural_key(nodes[i].name), nodes[i].name)):
            checkpoint(cancelled)
            node = nodes[child]
            if node.kind == "File" and not include_files:
                items.append(skip(node, "Loose file kept inside its parent."))
                continue
            if node.kind not in ("File", "Folder"):
                items.append(skip(node, "Link or unsupported entry left in place."))
                continue
            try:
                members = subtree_manifest(snapshot, child, cancelled)
            except ValueError as error:
                items.append(skip(node, str(error)))
                continue
            output = root / node.name
            base, counter = output, 2
            while path_key(output) in reserved or os.path.lexists(output):
                checkpoint(cancelled)
                if collision == "skip":
                    break
                stem = base.name if node.kind == "Folder" else base.stem
                suffix = "" if node.kind == "Folder" else base.suffix
                output = base.with_name(f"{stem} ({counter}){suffix}")
                counter += 1
            if collision == "skip" and (path_key(output) in reserved or os.path.lexists(output)):
                items.append(skip(node, "Destination is occupied or reserved; source stays in place.", output))
                continue
            error = validate_name(output.name)
            if error:
                items.append(FolderMove(node.path, str(output), "Conflict", error))
                continue
            reserved.add(path_key(output))
            spec = dict(source=node.path, output=str(output), name=node.name, kind=node.kind,
                        identity=list(node.identity), members=list(members), root=str(root),
                        root_identity=list(nodes[0].identity), parent=str(root),
                        parent_identity=list(nodes[0].identity), source_parent=parent.path,
                        source_parent_identity=list(parent.identity), remove_empty=bool(remove_empty))
            items.append(FolderMove(node.path, str(output), "Ready", "Move up one level; contents unchanged.", spec))
    return FlattenPlan(str(root), tuple(items))


def validate_move(spec):
    """Constrain even a saved or tampered plan to the supported two-level shape."""
    if spec.get("initial_state") == "skipped":
        root, source, parent = (Path(spec[k]) for k in ("root", "source", "source_parent"))
        if (not all(p.is_absolute() and ".." not in p.parts for p in (root, source, parent))
                or parent.parent != root or source.parent != parent):
            raise ValueError("Skipped inputs must remain children of an immediate parent inside the root.")
        return
    root, source, output = (Path(spec[k]) for k in ("root", "source", "output"))
    if not all(p.is_absolute() for p in (root, source, output)):
        raise ValueError("Folder moves require absolute paths.")
    if any(".." in p.parts for p in (root, source, output)):
        raise ValueError("Parent traversal is not allowed in a saved folder plan.")
    original_parent = Path(spec["source_parent"])
    if original_parent.parent != root or original_parent == root:
        raise ValueError("Source parent must be directly inside the root.")
    if spec.get("undo_of_job"):
        if source.parent != root or output.parent != original_parent:
            raise ValueError("Undo must restore a child to its exact original parent.")
    elif source.parent != original_parent or output.parent != root:
        raise ValueError("Flattening moves children exactly one level up.")
    if source == output or source == root or output == original_parent or validate_name(output.name):
        raise ValueError("Unsafe or invalid folder move destination.")
    if spec["kind"] not in ("Folder", "File") or not spec.get("members"):
        raise ValueError("Missing file identity or subtree manifest.")
    seen = set()
    for member in spec["members"]:
        relative = PurePosixPath(member["relative"])
        if (relative.is_absolute() or ".." in relative.parts or "\\" in member["relative"]
                or ":" in member["relative"] or member["relative"] in seen
                or member["kind"] not in ("Folder", "File")):
            raise ValueError("Invalid saved subtree manifest.")
        seen.add(member["relative"])
    first = next((m for m in spec["members"] if m["relative"] == "."), None)
    if first is None or first["kind"] != spec["kind"] or first["identity"] != spec["identity"]:
        raise ValueError("Subtree root identity is inconsistent.")


def check_root(spec):
    validate_move(spec)
    if directory_identity(spec["root"]) != spec["root_identity"]:
        raise ValueError("Root folder changed or was replaced. Nothing will be moved.")


def matches(path, spec, cancelled=lambda: False):
    path = Path(path)
    try:
        info = path.lstat()
        if linked(path, info):
            return False
        if spec["kind"] == "File":
            return stat.S_ISREG(info.st_mode) and entry_identity(info, "File") == spec["identity"]
        if not stat.S_ISDIR(info.st_mode) or entry_identity(info, "Folder") != spec["identity"]:
            return False
        snapshot = scan_folder(path, cancelled)
        if snapshot.cancelled:
            checkpoint(cancelled)
        if not snapshot.complete:
            return False
        actual = subtree_manifest(snapshot, 0, cancelled)
        return {m["relative"]: (m["kind"], m["identity"]) for m in actual} == {
            m["relative"]: (m["kind"], m["identity"]) for m in spec["members"]}
    except (OSError, ValueError):
        return False


def cleanup_rows(store, job_id):
    return [dict(r) for r in store.db.execute("SELECT * FROM folder_cleanup WHERE job_id=? ORDER BY path", (job_id,))]


def set_cleanup(store, job_id, path, state, message, restored=None):
    with store.db:
        if restored is not None:
            store.db.execute("UPDATE folder_cleanup SET state=?,message=?,restored_identity=? WHERE job_id=? AND path=?",
                             (state, message, json.dumps(restored), job_id, path))
        else:
            store.db.execute("UPDATE folder_cleanup SET state=?,message=? WHERE job_id=? AND path=?",
                             (state, message, job_id, path))
        store._event(job_id, None, "folder_" + state, f"{path}: {message}")


def destination_parent_identity(store, spec):
    if not spec.get("undo_of_job"):
        return spec["root_identity"]
    row = store.db.execute("SELECT * FROM folder_cleanup WHERE job_id=? AND path=?",
                           (spec["undo_of_job"], spec["source_parent"])).fetchone()
    if row and row["restored_identity"]:
        return json.loads(row["restored_identity"])
    return spec["source_parent_identity"]


def preflight_move(store, spec, phase, cancelled=lambda: False):
    check_root(spec)
    source, output = Path(spec["source"]), Path(spec["output"])
    source_id = spec["root_identity"] if spec.get("undo_of_job") else spec["source_parent_identity"]
    # After a recorded move the old parent may already have been removed.
    committed = phase in ("committing", "committed")
    if committed and matches(output, spec, cancelled):
        if os.path.lexists(source) and matches(source, spec, cancelled):
            raise ValueError("Both locations share the saved identity; recovery is ambiguous. Nothing was moved.")
        if directory_identity(output.parent) != destination_parent_identity(store, spec):
            raise ValueError("Destination parent changed after the move.")
        return "committed"
    if directory_identity(source.parent) != source_id:
        raise ValueError("Source parent was replaced. Nothing will be moved.")
    if not matches(source, spec, cancelled):
        raise ValueError(f"Source tree changed, moved or is missing: {source}. Refresh before starting a new plan.")
    if os.path.lexists(output):
        raise FileExistsError(f"Destination appeared after preview; kept untouched: {output}")
    if output.parent.exists():
        if directory_identity(output.parent) != destination_parent_identity(store, spec):
            raise ValueError("Destination parent was replaced.")
    elif not spec.get("undo_of_job"):
        raise ValueError("Destination folder is missing.")
    else:
        row = store.db.execute("SELECT state FROM folder_cleanup WHERE job_id=? AND path=?",
                               (spec["undo_of_job"], str(output.parent))).fetchone()
        if not row or row["state"] not in ("removed", "removing", "recreating"):
            raise ValueError("Original parent is missing without a recorded removal. It was not recreated.")
    return "pending"


def recreate_parents(store, specs, hook, cancelled=lambda: False):
    for spec in specs:
        checkpoint(cancelled)
        if not spec.get("undo_of_job"):
            continue
        check_root(spec)
        parent = Path(spec["output"]).parent
        if os.path.lexists(parent):
            if directory_identity(parent) != destination_parent_identity(store, spec):
                raise ValueError("An unrelated folder occupies an original parent path.")
            continue
        original = spec["undo_of_job"]
        row = store.db.execute("SELECT state FROM folder_cleanup WHERE job_id=? AND path=?", (original, str(parent))).fetchone()
        if not row or row["state"] not in ("removed", "removing", "recreating"):
            raise ValueError("Missing parent has no matching cleanup record.")
        set_cleanup(store, original, str(parent), "recreating", "Preparing to recreate the removed empty parent.")
        hook("mkdir_intent")
        checkpoint(cancelled)
        parent.mkdir()  # No exist_ok: never adopt an unrelated folder.
        hook("mkdir_created")
        restored = directory_identity(parent)
        set_cleanup(store, original, str(parent), "recreated", "Recreated for undo; original directory metadata is not restored.", restored)
        sync_directory(str(parent.parent))
        hook("mkdir_recorded")


def clean_empty_parents(store, job_id, specs, hook=lambda point: None):
    if any(i["state"] not in ("succeeded", "skipped") for i in store.items(job_id)):
        return
    if not specs or specs[0].get("undo_of_job"):
        return
    for row in cleanup_rows(store, job_id):
        if row["state"] not in ("pending", "removing"):
            continue
        path = Path(row["path"])
        try:
            check_root(specs[0])
            if row["state"] == "removing" and not os.path.lexists(path):
                set_cleanup(store, job_id, str(path), "removed", "Recovered an empty-parent removal recorded before interruption.")
                continue
            if directory_identity(path) != json.loads(row["identity"]):
                raise ValueError("Parent identity changed; kept untouched.")
            with os.scandir(path) as entries:
                if next(entries, None) is not None:
                    set_cleanup(store, job_id, str(path), "kept", "Parent still contains entries; kept untouched.")
                    continue
            set_cleanup(store, job_id, str(path), "removing", "Removing only this verified empty parent.")
            hook("rmdir_intent")
            if directory_identity(path) != json.loads(row["identity"]):
                raise ValueError("Parent changed immediately before removal; kept untouched.")
            path.rmdir()  # Nonrecursive; newly arrived content makes this fail safely.
            hook("rmdir_removed")
            set_cleanup(store, job_id, str(path), "removed", "Empty parent removed nonrecursively.")
            sync_directory(str(path.parent))
        except (OSError, ValueError) as error:
            current = store.db.execute("SELECT state FROM folder_cleanup WHERE job_id=? AND path=?", (job_id, str(path))).fetchone()
            removed = current["state"] in ("removing", "removed") and not os.path.lexists(path)
            set_cleanup(store, job_id, str(path), "removed" if removed else "kept",
                        f"Empty parent removal completed; durability check reported: {error}" if removed else
                        f"Moves retained; parent was not removed: {error}")


def execute_flatten_job(store, job_id, cancelled, paused, progress, hook=lambda point: None):
    items = store.items(job_id)
    specs = [json.loads(i["spec"]) for i in items]
    active = [i for i in items if i["state"] == "queued"]
    store.set_job(job_id, "running")
    try:
        # Whole-batch preflight precedes any mutation, including undo mkdir.
        for item, spec in zip(items, specs):
            checkpoint(cancelled)
            if item["state"] in ("queued", "succeeded"):
                if item['state']=='queued' and item['rename_phase']=='pending':
                    from .workflows import guard_workflow_inputs
                    guard_workflow_inputs(store,spec['source'],[],job_id,directory=spec.get('kind')=='Folder')
                preflight_move(store, spec, item["rename_phase"], cancelled)
        if paused():
            return store.summarize(job_id)
        checkpoint(cancelled)
        if active:
            store.set_item(active[0]["id"], "running", "Preparing folder move batch")
        recreate_parents(store, [json.loads(i["spec"]) for i in active], hook, cancelled)
    except Exception as error:
        for item in active:
            store.set_item(item["id"], "cancelled" if isinstance(error, Cancelled) else "failed", str(error))
        return store.summarize(job_id)
    done = sum(i["state"] in ("succeeded", "skipped") for i in items)
    for item in active:
        if paused():
            break
        if cancelled():
            store.cancel_queued(job_id)
            break
        spec = json.loads(item["spec"])
        try:
            position = preflight_move(store, spec, item["rename_phase"], cancelled)
            store.set_item(item["id"], "running", "Checking source tree", attempts=item["attempts"] + 1)
            if position != "committed":
                if directory_identity(Path(spec["output"]).parent) != destination_parent_identity(store, spec):
                    raise ValueError("Destination parent changed before moving.")
                if Path(spec["source"]).lstat().st_dev != Path(spec["output"]).parent.lstat().st_dev:
                    raise ValueError("Cross-volume moves are not supported; nothing was copied or deleted.")
                store.set_item(item["id"], "committing", "Moving without replacement", rename_phase="committing")
                hook("move_intent")
                preflight_move(store, spec, "pending", cancelled)
                checkpoint(cancelled)
                move_no_replace(spec["source"], spec["output"])
                hook("moved")
                sync_directory(str(Path(spec["source"]).parent))
                if not matches(spec["output"], spec, cancelled):
                    raise ValueError("Moved content changed during verification. Both locations were left untouched; inspect and recover this job.")
            store.set_item(item["id"], "succeeded",
                           "Restored original location; contents unchanged." if spec.get("undo_of_job") else "Moved up one level; contents unchanged.",
                           rename_phase="committed")
            hook("move_recorded")
            done += 1
            progress(item["id"], done, len(items))
        except Exception as error:
            # Keep the intent: retry can recognize a completed atomic move by its
            # original identity and exact subtree, never by filename alone.
            store.set_item(item["id"], "cancelled" if isinstance(error, Cancelled) else "failed", str(error))
            break
    # If a failure stopped this batch, don't let unrelated queued children start
    # automatically; retry rechecks the entire remaining frozen plan.
    failures = [i for i in store.items(job_id) if i["state"] in ("failed", "cancelled", "interrupted")]
    if failures:
        state = "cancelled" if failures[0]["state"] == "cancelled" else "interrupted"
        for item in store.items(job_id):
            if item["state"] == "queued":
                store.set_item(item["id"], state, "Batch stopped. Retry checks completed moves before continuing.")
    checkpoint_allowed = not cancelled() and not paused()
    if checkpoint_allowed:
        clean_empty_parents(store, job_id, specs, hook)
    elif active and all(i["state"] in ("succeeded", "skipped") for i in store.items(job_id)) and any(
            r["state"] in ("pending", "removing") for r in cleanup_rows(store, job_id)):
        store.set_item(active[-1]["id"], "interrupted", "Moves finished; retry to complete the pending empty-parent review.")
    return store.summarize(job_id)


def inverse_folder_specs(store, job_id):
    job = next((j for j in store.jobs() if j["id"] == job_id), None)
    if not job or job["adapter"] != "flatten":
        raise ValueError("Select a completed flatten job to undo.")
    if store.original_job(job_id):
        raise ValueError("This is already an undo job. Redo is not available.")
    if store.undo_job(job_id):
        raise ValueError("An undo already exists. Review or retry it in Queue.")
    items = store.items(job_id)
    if not items or any(i["state"] not in ("succeeded", "skipped") for i in items):
        raise ValueError("Finish or retry the original batch before undoing it.")
    result = []
    for item in reversed(items):
        if item["state"] != "succeeded":
            continue
        if item["rename_phase"] != "committed":
            raise ValueError("Recover the unconfirmed move before undoing it.")
        original = json.loads(item["spec"])
        spec = dict(original, source=original["output"], output=original["source"],
                    name=Path(original["output"]).name, remove_empty=False,
                    undo_of_job=job_id, undo_of_item=item["id"])
        validate_move(spec)
        result.append(spec)
    if not result:
        raise ValueError("This job has no completed moves to undo.")
    return tuple(result)


def plan_folder_undo(store, job_id, cancelled=lambda: False):
    specs = inverse_folder_specs(store, job_id)
    items = []
    for spec in specs:
        checkpoint(cancelled)
        try:
            preflight_move(store, spec, "pending", cancelled)
            missing = not Path(spec["output"]).parent.exists()
            reason = "Recreate the removed empty parent and restore the original location." if missing else "Restore the exact original location."
            items.append(FolderMove(spec["source"], spec["output"], "Ready", reason, spec))
        except (OSError, ValueError) as error:
            items.append(FolderMove(spec["source"], spec["output"], "Conflict", str(error), spec))
    return FlattenPlan(specs[0]["root"], tuple(items), job_id)


def projected_snapshot(snapshot, plan, cancelled=lambda: False):
    """Cached before/after tree; no filesystem reads or writes."""
    moves = {i.source: i.output for i in plan.items if i.status == "Ready"}
    if plan.original_job:
        return None  # Undo preview uses the exact before/after path table.
    mapping, paths = {}, []
    for index, node in enumerate(snapshot.nodes):
        checkpoint(cancelled)
        path = Path(node.path)
        if node.depth >= 2:
            ancestor = Path(snapshot.nodes[index].path).relative_to(snapshot.root).parts[:2]
            original = str(Path(snapshot.root).joinpath(*ancestor))
            if original in moves:
                path = Path(moves[original]) / Path(node.path).relative_to(original)
        mapping[node.path] = str(path)
        paths.append((node, str(path)))
    removable = {s["source_parent"] for s in plan.specs if s.get("remove_empty")}
    still_used = {str(Path(path).parent) for node, path in paths if node.depth > 0}
    # Rebuild in parent-before-child order, not alphabetical recursive traversal.
    by_path = {p: n for n, p in paths if p not in removable or p in still_used}
    ordered = sorted(by_path, key=lambda p: len(Path(p).parts))
    ids, nodes, children = {}, [], []
    for path in ordered:
        checkpoint(cancelled)
        node = by_path[path]
        parent = ids.get(str(Path(path).parent), -1)
        index = len(nodes)
        ids[path] = index
        nodes.append(replace(node, path=path, name=Path(path).name or path, parent=parent,
                             depth=len(Path(path).relative_to(snapshot.root).parts)))
        children.append([])
        if parent >= 0:
            children[parent].append(index)
    return aggregate(snapshot.root, nodes, children, snapshot.issues)
