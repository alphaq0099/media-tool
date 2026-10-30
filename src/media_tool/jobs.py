"""Durable sequential jobs. No Qt, media transforms, or shell commands here."""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import time
import uuid

from .domain import path_key
from .planning import validate_name

SCHEMA = 22
ACTIVE = ("running", "verifying", "committing")
INCOMPLETE = ("failed", "interrupted", "cancelled", "blocked")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def identity(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def regular(path):
    info = Path(path).lstat()
    if not stat.S_ISREG(info.st_mode) or Path(path).is_symlink():
        raise ValueError("Expected an ordinary file; links are not supported.")
    if not info.st_ino:
        raise ValueError("This filesystem does not provide stable file identities; verified copies are unavailable.")
    return info


def check_source(spec, handle=None):
    current = regular(spec["source"])
    if identity(current) != spec["identity"]:
        raise ValueError("Source changed since this job was queued. Add it again.")
    if handle is not None and identity(os.fstat(handle.fileno())) != spec["identity"]:
        raise ValueError("Source was replaced while opening it.")
    return current


def copy_snapshot(records, destination, cancelled=lambda: False):
    """Read-only frozen manifest; no destination directories or files are created."""
    target = Path(destination)
    if not target.is_absolute() or not target.is_dir():
        raise ValueError("Choose an existing output folder.")
    target = target.resolve(strict=True)
    if not target.stat().st_ino:
        raise ValueError("The output filesystem does not provide stable file identities.")
    reserved, seen, result, next_suffix = set(), set(), [], {}
    for record in sorted(records, key=lambda item: item.sort_key):
        checkpoint(cancelled)
        if record.sample:
            raise ValueError("Sample rows are not real files. Add real files first.")
        if record.identity_key in seen:
            continue
        seen.add(record.identity_key)
        error = validate_name(record.name)
        if error:
            raise ValueError(error)
        source = Path(record.path)
        info = regular(source)
        original = [record.device, record.inode, record.size, record.modified_ns]
        if identity(info) != original:
            raise ValueError(f"Source changed: {record.name}. Add it again.")
        output = target / record.name
        base = path_key(output)
        number = next_suffix.get(base, 2)
        while path_key(output) in reserved or os.path.lexists(output):
            output = target / f"{source.stem} ({number}){source.suffix}"
            number += 1
        next_suffix[base] = number
        error = validate_name(output.name)
        if error:
            raise ValueError(error)
        reserved.add(path_key(output))
        result.append({"source": str(source), "output": str(output), "identity": original,
                       "parent": str(target), "parent_identity": list(identity(target.stat())[:2]),
                       "name": record.name})
    if not result:
        raise ValueError("There are no files to copy.")
    return tuple(result)


class Store:
    """Each thread opens its own connection. The UI holds the exclusive owner lock."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "history.sqlite3"
        self.db = sqlite3.connect(self.path, timeout=3)
        self.db.row_factory = sqlite3.Row
        try:
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version not in range(SCHEMA + 1):
                raise ValueError(f"Unsupported job history version {version}. History was left intact.")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("BEGIN IMMEDIATE")
            with self.db:
                self.db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, adapter TEXT NOT NULL,
                    created TEXT NOT NULL, state TEXT NOT NULL)""")
                self.db.execute("""CREATE TABLE IF NOT EXISTS items (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
                    ordinal INTEGER NOT NULL, spec TEXT NOT NULL, state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '',
                    temp TEXT, temp_dev TEXT, temp_ino TEXT, digest TEXT,
                    updated TEXT NOT NULL, stage TEXT,
                    rename_phase TEXT NOT NULL DEFAULT 'pending')""")
                self.db.execute("""CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY, job_id TEXT NOT NULL,
                    item_id TEXT, time TEXT NOT NULL, state TEXT NOT NULL, message TEXT NOT NULL)""")
                self.db.execute("""CREATE TABLE IF NOT EXISTS workflow_links (
                    parent_job TEXT NOT NULL REFERENCES jobs(id),
                    step_item TEXT PRIMARY KEY REFERENCES items(id),
                    child_job TEXT UNIQUE NOT NULL REFERENCES jobs(id))""")
                self.db.execute("""CREATE TABLE IF NOT EXISTS queue_order (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(id), position INTEGER NOT NULL)""")
                self.db.execute("""INSERT OR IGNORE INTO queue_order
                    SELECT id, ROW_NUMBER() OVER (ORDER BY created,id) FROM jobs""")
                self.db.execute("""CREATE TABLE IF NOT EXISTS job_runs (
                    id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
                    started TEXT NOT NULL, ended TEXT, seconds REAL NOT NULL DEFAULT 0)""")
                self.db.execute('CREATE INDEX IF NOT EXISTS items_job_order ON items(job_id,ordinal)')
                self.db.execute('CREATE INDEX IF NOT EXISTS events_job_time ON events(job_id,time)')
                self.db.execute('CREATE INDEX IF NOT EXISTS workflow_parent ON workflow_links(parent_job)')
                self.db.execute('CREATE INDEX IF NOT EXISTS runs_job ON job_runs(job_id)')
                self.db.execute("""CREATE TABLE IF NOT EXISTS rename_undos (
                    original_job TEXT PRIMARY KEY REFERENCES jobs(id),
                    undo_job TEXT NOT NULL UNIQUE REFERENCES jobs(id))""")
                self.db.execute("""CREATE TABLE IF NOT EXISTS folder_cleanup (
                    job_id TEXT NOT NULL REFERENCES jobs(id), path TEXT NOT NULL,
                    identity TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                    restored_identity TEXT, message TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(job_id,path))""")
                columns = {row[1] for row in self.db.execute("PRAGMA table_info(items)")}
                if "stage" not in columns:
                    self.db.execute("ALTER TABLE items ADD COLUMN stage TEXT")
                if "rename_phase" not in columns:
                    self.db.execute("ALTER TABLE items ADD COLUMN rename_phase TEXT NOT NULL DEFAULT 'pending'")
                for column in ("source_digest", "original_cleanup"):
                    if column not in columns:
                        self.db.execute(f"ALTER TABLE items ADD COLUMN {column} TEXT")
                self.db.execute(f"PRAGMA user_version={SCHEMA}")
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def _event(self, job_id, item_id, state, message):
        self.db.execute("INSERT INTO events(job_id,item_id,time,state,message) VALUES (?,?,?,?,?)",
                        (job_id, item_id, now(), state, message))

    def enqueue(self, title, adapter, specs):
        with self.db:
            return self._enqueue(title, adapter, specs)

    def _enqueue(self, title, adapter, specs):
        """Insert inside the caller's transaction, including any undo relationship."""
        if adapter not in ("sample", "copy", "rename", "images", "video", "audio", "documents", "archives", "flatten", "workflow"):
            raise ValueError("This operation is not enabled yet.")
        # JSON round trip is the boundary: subsequent UI changes cannot mutate this plan.
        snapshots = json.loads(json.dumps(list(specs)))
        if not snapshots:
            raise ValueError("A job needs at least one item.")
        if adapter=='workflow':
            from .workflows import validate_specs
            validate_specs(snapshots)
        if adapter == "flatten":
            from .folder_moves import validate_move
            for spec in snapshots:
                validate_move(spec)
                if spec.get("initial_state", "queued") not in ("queued", "skipped"):
                    raise ValueError("New folder items must be queued or explicitly skipped.")
            moving = [s for s in snapshots if s.get("initial_state") != "skipped"]
            if (len({os.path.normcase(os.path.abspath(s["source"])) for s in snapshots}) != len(snapshots)
                    or len({path_key(s["output"]) for s in moving}) != len(moving)
                    or len({s["root"] for s in snapshots}) != 1):
                raise ValueError("Folder jobs require unique sources/outputs inside one root.")
        job_id = uuid.uuid4().hex
        self.db.execute("INSERT INTO jobs VALUES (?,?,?,?,?)",
                        (job_id, title, adapter, now(), "queued"))
        self.db.execute("INSERT INTO queue_order SELECT ?,COALESCE(MAX(position),0)+1 FROM queue_order", (job_id,))
        for index, spec in enumerate(snapshots):
            item_id = uuid.uuid4().hex
            stage = str(Path(spec["parent"]) / f".mediatool-rename-{job_id}-{item_id}.stage") if adapter == "rename" else None
            state = spec.get("initial_state", "queued") if adapter in ("rename", "images", "video", "audio", "documents", "archives", "flatten") else "queued"
            self.db.execute("""INSERT INTO items
                (id,job_id,ordinal,spec,state,updated,stage,message) VALUES (?,?,?,?,?,?,?,?)""",
                (item_id, job_id, index, json.dumps(spec), state, now(), stage,
                 spec.get("initial_message", "") if adapter in ("rename", "images", "video", "audio", "documents", "archives", "flatten") else ""))
            if adapter == "flatten" and spec.get("remove_empty") and not spec.get("undo_of_job"):
                self.db.execute("INSERT OR IGNORE INTO folder_cleanup(job_id,path,identity) VALUES (?,?,?)",
                                (job_id, spec["source_parent"], json.dumps(spec["source_parent_identity"])))
        self._event(job_id, None, "queued", f"{len(snapshots)} items")
        return job_id

    def undo_job(self, original_job):
        row = self.db.execute("SELECT undo_job FROM rename_undos WHERE original_job=?", (original_job,)).fetchone()
        return row[0] if row else None

    def original_job(self, undo_job):
        row = self.db.execute("SELECT original_job FROM rename_undos WHERE undo_job=?", (undo_job,)).fetchone()
        return row[0] if row else None

    def enqueue_undo(self, original_job, specs):
        from .undo import inverse_specs
        job = next((j for j in self.jobs() if j["id"] == original_job), None)
        folder = job is not None and job["adapter"] == "flatten"
        if folder:
            from .folder_moves import inverse_folder_specs
        # Acquire the writer lock before checking eligibility and linking the job.
        self.db.execute("BEGIN IMMEDIATE")
        with self.db:
            expected = inverse_folder_specs(self, original_job) if folder else inverse_specs(self, original_job)
            if json.loads(json.dumps(list(specs))) != json.loads(json.dumps(expected)):
                raise ValueError("Undo preview no longer matches saved history. Preview again.")
            job_id = self._enqueue("Undo flatten" if folder else "Undo rename", "flatten" if folder else "rename", expected)
            self.db.execute("INSERT INTO rename_undos VALUES (?,?)", (original_job, job_id))
            self._event(job_id, None, "queued", f"Restores original {'locations' if folder else 'names'} from job {original_job}")
        return job_id

    def jobs(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM jobs ORDER BY created,id")]

    def visible_jobs(self):
        return [dict(row) for row in self.db.execute("""SELECT jobs.* FROM jobs
            JOIN queue_order ON jobs.id=queue_order.job_id
            WHERE jobs.id NOT IN (SELECT child_job FROM workflow_links)
            ORDER BY position,created,jobs.id""")]

    def move_waiting(self, job_id, direction):
        from .queue_history import move_waiting
        return move_waiting(self, job_id, direction)

    def clear_completed(self, preview=False, protected_jobs=()):
        from .queue_history import clear_completed
        return clear_completed(self, preview, protected_jobs)

    def begin_run(self, job_id):
        with self.db:
            return self.db.execute('INSERT INTO job_runs(job_id,started) VALUES (?,?)', (job_id, now())).lastrowid

    def end_run(self, run_id, seconds):
        with self.db:
            self.db.execute('UPDATE job_runs SET ended=?,seconds=? WHERE id=? AND ended IS NULL',
                            (now(), max(0, seconds), run_id))

    def elapsed_times(self, live=False, job_id=None):
        where=' WHERE job_id=?' if job_id else ''
        parameters=(job_id,) if job_id else ()
        values = {r['job_id']:r['seconds'] for r in self.db.execute(
            'SELECT job_id,SUM(seconds) AS seconds FROM job_runs'+where+' GROUP BY job_id',parameters)}
        if live:
            current=datetime.now(timezone.utc)
            for row in self.db.execute('SELECT job_id,started FROM job_runs WHERE ended IS NULL'+(' AND job_id=?' if job_id else ''),parameters):
                values[row['job_id']]=values.get(row['job_id'],0)+max(0,(current-datetime.fromisoformat(row['started'])).total_seconds())
        return values

    def items(self, job_id):
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM items WHERE job_id=? ORDER BY ordinal", (job_id,))]

    def item(self, item_id):
        return dict(self.db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone())

    def events(self, job_id):
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM events WHERE job_id=? ORDER BY sequence", (job_id,))]

    def set_job(self, job_id, state):
        with self.db:
            self.db.execute("UPDATE jobs SET state=? WHERE id=?", (state, job_id))

    def set_item(self, item_id, state, message="", **fields):
        allowed = {"attempts", "temp", "temp_dev", "temp_ino", "digest", "stage", "rename_phase", "source_digest", "original_cleanup"}
        if not fields.keys() <= allowed:
            raise ValueError("Unsupported history field")
        row = self.item(item_id)
        updates = {"state": state, "message": message, "updated": now(), **fields}
        with self.db:
            sql = ",".join(key + "=?" for key in updates)
            self.db.execute("UPDATE items SET " + sql + " WHERE id=?", (*updates.values(), item_id))
            self._event(row["job_id"], item_id, state, message)
        from .activity import publish
        publish(row['job_id'],item_id,state,message)

    def summarize(self, job_id):
        counts = Counter(item["state"] for item in self.items(job_id))
        workflow=self.db.execute('SELECT adapter FROM jobs WHERE id=?',(job_id,)).fetchone()[0]=='workflow'
        if workflow and not any(counts[state] for state in ACTIVE):
            for state in ('failed','interrupted','cancelled','blocked'):
                if counts[state]:
                    self.set_job(job_id,state)
                    return state
        if any(counts[state] for state in ACTIVE):
            state = "running"
        elif counts["queued"]:
            state = "queued"
        elif counts["failed"]:
            state = "failed"
        elif counts["interrupted"]:
            state = "interrupted"
        elif counts["cancelled"]:
            state = "cancelled"
        elif counts["succeeded"] and not counts["skipped"]:
            state = "succeeded"
        else:
            state = "completed with skips"
        self.set_job(job_id, state)
        return state

    def recover(self):
        # No hashing or filesystem mutations on the GUI thread. Recovery verification
        # happens on retry, using the persisted temporary file identity and digest.
        # A crashed run ends at its last durable event, never at the next launch.
        from .queue_history import recover_runs
        recover_runs(self)
        rows = list(self.db.execute("SELECT id FROM items WHERE state IN (?,?,?)", ACTIVE))
        for row in rows:
            self.set_item(row["id"], "interrupted",
                          "App stopped during work. Retry checks saved output before restarting.")
        for job in self.jobs():
            if job["adapter"] == "rename" and any(item["state"] == "interrupted" for item in self.items(job["id"])):
                for item in self.items(job["id"]):
                    if item["state"] == "queued":
                        self.set_item(item["id"], "interrupted", "Rename batch interrupted. Retry the batch to reconcile saved names.")
            if job["adapter"] == "flatten":
                items = self.items(job["id"])
                interrupted = any(i["state"] == "interrupted" for i in items)
                for item in items:
                    if interrupted and item["state"] == "queued":
                        self.set_item(item["id"], "interrupted", "Folder batch interrupted. Retry to reconcile completed moves.")
                unfinished_cleanup = self.db.execute(
                    "SELECT 1 FROM folder_cleanup WHERE job_id=? AND state IN ('pending','removing') LIMIT 1", (job["id"],)).fetchone()
                if unfinished_cleanup and items and all(i["state"] in ("succeeded", "skipped") for i in items):
                    last = next((i for i in reversed(items) if i["state"] == "succeeded"), None)
                    if last:
                        self.set_item(last["id"], "interrupted", "Moves finished before parent review. Retry safely finishes the recorded cleanup.")
            self.summarize(job["id"])
        return len(rows)

    def retry(self, job_id):
        if any(item["state"] in ACTIVE for item in self.items(job_id)):
            raise ValueError("Wait for this job to stop before retrying.")
        count = 0
        for item in self.items(job_id):
            if item["state"] in INCOMPLETE:
                self.set_item(item["id"], "queued", "Queued for a fresh attempt; saved output is checked first.")
                count += 1
        self.summarize(job_id)
        return count

    def cancel_queued(self, job_id):
        workflow=self.db.execute('SELECT adapter FROM jobs WHERE id=?',(job_id,)).fetchone()[0]=='workflow'
        if workflow:
            self.db.execute('BEGIN IMMEDIATE')
            with self.db:
                children=[r[0] for r in self.db.execute('SELECT child_job FROM workflow_links WHERE parent_job=?',(job_id,))]
                groups={key:self.items(key) for key in [*children,job_id]}
                if any(i['state'] in ACTIVE for rows in groups.values() for i in rows):
                    raise ValueError('Request cancellation of the running workflow and wait for it to stop.')
                message='Workflow cancelled. Completed outputs and recovery evidence kept.'
                for key,rows in groups.items():
                    unfinished=[i for i in rows if i['state'] in ('queued',*INCOMPLETE)]
                    for item in unfinished:
                        self.db.execute('UPDATE items SET state=?,message=?,updated=? WHERE id=?',('cancelled',message,now(),item['id']))
                        self._event(key,item['id'],'cancelled',message)
                    if unfinished:self.db.execute('UPDATE jobs SET state=? WHERE id=?',('cancelled',key))
            return
        for item in self.items(job_id):
            if item["state"] == "queued":
                self.set_item(item["id"], "cancelled", "Cancelled before starting.")
        self.summarize(job_id)


class OwnerLock:
    """OS lock, released on exit/crash. No stale PID or lock-file deletion guessing."""
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.file = (directory / "queue.lock").open("a+b")
        try:
            self.file.seek(0, os.SEEK_END)
            if self.file.tell() == 0:
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.file.close()
            raise RuntimeError("Queue is already open in another app instance.")

    def close(self):
        if not self.file.closed:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.file.close()


class Cancelled(Exception):
    pass


def checkpoint(cancelled):
    if cancelled():
        raise Cancelled("Cancelled. Original files are unchanged.")


def hash_file(path, cancelled=lambda: False, progress=lambda done, total: None):
    digest = hashlib.sha256()
    done = 0
    with Path(path).open("rb") as handle:
        total = os.fstat(handle.fileno()).st_size
        while chunk := handle.read(1024 * 1024):
            checkpoint(cancelled)
            digest.update(chunk)
            done += len(chunk)
            progress(done, total)
    checkpoint(cancelled)
    return digest.hexdigest()


def owns_temp(item, path=None):
    candidate = Path(path or item["temp"]) if (path or item["temp"]) else None
    if candidate is None or item["temp_dev"] is None:
        return False
    try:
        info = regular(candidate)
        return (str(info.st_dev), str(info.st_ino)) == (item["temp_dev"], item["temp_ino"])
    except (OSError, ValueError):
        return False


def cleanup_temp(store, item_id):
    item = store.item(item_id)
    if item["temp"] and owns_temp(item):
        Path(item["temp"]).unlink()
        return True
    return False


def sync_directory(directory):
    if os.name != "nt":
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def commit_no_replace(temp, output):
    if os.name == "nt":
        os.rename(temp, output)  # Windows rename fails if the final name already exists.
    else:
        # Same-volume hard link is atomic and refuses existing destinations.
        # Unsupported filesystems fail safely rather than falling back to overwrite.
        os.link(temp, output, follow_symlinks=False)
        os.unlink(temp)
    sync_directory(str(Path(output).parent))


def run_copy(store, item, cancelled, progress, hook=lambda point: None):
    spec = json.loads(item["spec"])
    output = Path(spec["output"])
    parent = Path(spec["parent"])
    if str(output.parent) != str(parent) or list(identity(parent.stat())[:2]) != spec["parent_identity"]:
        raise ValueError("Output folder was replaced or moved. Queue a new plan.")
    if parent.resolve(strict=True) != parent:
        raise ValueError("Output folder now resolves through a link. Queue a new plan.")

    # A crash after the atomic commit but before recording success is recoverable.
    # Hash equality alone never establishes ownership of an existing output.
    if item["digest"] and owns_temp(item, output):
        if hash_file(output, cancelled, progress) != item["digest"]:
            raise ValueError("Saved output changed after interruption; it was left untouched.")
        cleanup_temp(store, item["id"])
        return "Recovered and verified the previously committed copy."
    if item["temp"] and os.path.lexists(item["temp"]):
        if not cleanup_temp(store, item["id"]):
            raise ValueError("A saved temporary path now belongs to another file; it was left untouched.")
    checkpoint(cancelled)
    check_source(spec)
    if os.path.lexists(output):
        raise FileExistsError("Output appeared after preview; existing file was kept.")

    temp = parent / f".mediatool-{item['job_id']}-{item['id']}-{uuid.uuid4().hex}.part"
    # Persist ownership immediately after exclusive creation. An abrupt crash in the
    # tiny create/journal gap may leave an untracked .part; never glob-delete these.
    with temp.open("xb") as destination:
        info = os.fstat(destination.fileno())
        store.set_item(item["id"], "running", "Copying",
                       temp=str(temp), temp_dev=str(info.st_dev), temp_ino=str(info.st_ino), digest=None)
        hook("created")
        digest = hashlib.sha256()
        done = 0
        with Path(spec["source"]).open("rb") as source:
            check_source(spec, source)
            while chunk := source.read(1024 * 1024):
                checkpoint(cancelled)
                destination.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                progress(done, spec["identity"][2])
                hook("chunk")
            check_source(spec, source)
        destination.flush()
        os.fsync(destination.fileno())
    hook("written")
    check_source(spec)
    checkpoint(cancelled)
    store.set_item(item["id"], "verifying", "Verifying SHA-256")
    if hash_file(temp, cancelled, progress) != digest.hexdigest():
        raise ValueError("Copy verification failed.")
    check_source(spec)
    checkpoint(cancelled)
    verified_identity = identity(regular(temp))
    # FULL synchronous SQLite commit saves the evidence before the final rename.
    store.set_item(item["id"], "committing", "Verified; committing output", digest=digest.hexdigest())
    hook("prepared")
    checkpoint(cancelled)
    if list(identity(parent.stat())[:2]) != spec["parent_identity"] or parent.resolve(strict=True) != parent:
        raise ValueError("Output folder changed while copying.")
    check_source(spec)
    if identity(regular(temp)) != verified_identity:
        raise ValueError("Temporary copy changed after verification; it was not committed.")
    commit_no_replace(temp, output)
    hook("committed")
    return "Copied and verified SHA-256. Original kept."


def execute_job(store, job_id, cancelled=lambda: False, paused=lambda: False,
                progress=lambda item_id, done, total: None, hook=lambda point: None):
    job = next(job for job in store.jobs() if job["id"] == job_id)
    if job['adapter']=='workflow':
        from .workflows import execute_workflow
        return execute_workflow(store,job_id,cancelled,paused,progress,hook)
    if job["adapter"] == "rename":
        from .renaming import execute_rename_job
        return execute_rename_job(store, job_id, cancelled, paused, progress, hook)
    if job["adapter"] == "flatten":
        from .folder_moves import execute_flatten_job
        return execute_flatten_job(store, job_id, cancelled, paused, progress, hook)
    store.set_job(job_id, "running")
    for item in store.items(job_id):
        if item["state"] != "queued":
            continue
        if cancelled():
            store.cancel_queued(job_id)
            break
        if paused():
            break
        attempts = item["attempts"] + 1
        store.set_item(item["id"], "running", "Starting", attempts=attempts)
        item = store.item(item["id"])
        try:
            spec = json.loads(item["spec"])
            if job["adapter"] == "sample":
                for step in range(40):
                    checkpoint(cancelled)
                    time.sleep(spec.get("seconds", 0.8) / 40)
                    progress(item["id"], step + 1, 40)
                if spec.get("fail_once") and attempts == 1:
                    raise ValueError("Intentional sample failure. Retry this item to complete it.")
                message = "Sample completed; no media files were read or written."
            elif job["adapter"] == "copy":
                message = run_copy(store, item, cancelled,
                                   lambda done, total: progress(item["id"], done, total), hook)
            elif job["adapter"] == "images":
                from .images import run_image
                message = run_image(store, item, cancelled,
                                    lambda done, total: progress(item["id"], done, total), hook)
            elif job["adapter"] == "archives":
                from .zip_archive import run_archive
                message = run_archive(store, item, cancelled,
                                      lambda done, total: progress(item["id"], done, total), hook)
            elif job["adapter"] == "documents":
                from .documents import run_document
                message = run_document(store,item,cancelled,lambda done,total:progress(item["id"],done,total),hook)
            elif job["adapter"] == "audio":
                from .audio import run_audio
                message = run_audio(store, item, cancelled,
                                    lambda done, total: progress(item["id"], done, total), hook)
            elif job["adapter"] == "video":
                from .video import run_video
                message = run_video(store, item, cancelled,
                                    lambda done, total: progress(item["id"], done, total), hook)
            else:
                raise ValueError("Unsupported operation. No files were changed.")
            if job["adapter"] in ("images", "video") and not spec.get("keep_originals", True):
                from .original_cleanup import cleanup_original
                cleanup_message = cleanup_original(store, item["id"], cancelled, hook)
                message = message.replace("Original kept.", "") + " " + cleanup_message
            store.set_item(item["id"], "succeeded", message)
        except Exception as error:
            # Never remove a committed output, even if history/disk writes failed.
            state = "cancelled" if isinstance(error, Cancelled) else "failed"
            try:
                cleanup_temp(store, item["id"])
            except OSError as cleanup_error:
                error = RuntimeError(f"{error} Temporary cleanup also failed: {cleanup_error}")
            store.set_item(item["id"], state, str(error) or type(error).__name__)
            if state == "cancelled":
                store.cancel_queued(job_id)
                break
    return store.summarize(job_id)
