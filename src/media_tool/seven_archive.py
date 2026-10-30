"""Optional 7-Zip adapter. Secrets never enter job history or tool diagnostics."""
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import stat
import subprocess
import threading
import uuid
import sys

from .jobs import checkpoint, identity
from .tool_paths import resolve_tool

_passwords = {}
_lock = threading.Lock()


def remember_password(password, token=None):
    if not password or any(ord(c) < 32 or ord(c) == 127 for c in password) or len(password) > 1024:
        raise ValueError("Enter a password of 1–1024 characters without control characters.")
    token = token or uuid.uuid4().hex
    with _lock:
        _passwords[token] = password
    return token


def password_for(spec):
    if not spec.get("encrypted"):
        return None
    with _lock:
        value = _passwords.get(spec.get("password_token"))
    if value is None:
        raise ValueError("Password required. Start this archive from Queue and enter its password again.")
    return value


def forget_password(token):
    with _lock:
        _passwords.pop(token, None)


def seven_run(arguments, password, cancelled, consume=None, cwd=None):
    tool = resolve_tool("7z")
    if not tool:
        raise ValueError("7-Zip was not found. Install 7-Zip and retry.")
    checkpoint(cancelled)
    # Bare -p means an empty password during reads; omit it to request a prompt.
    args = [arg for arg in arguments if not (
        arg == "-p" and password is not None
        and arguments[0] in ("l", "t", "x", "e"))]
    command = [tool, *args]
    private_console = os.name == 'nt' and password is not None
    tree = None
    if private_console:
        worker = ([sys.executable, '--archive-worker'] if getattr(sys, 'frozen', False) else
                  [sys.executable, str(Path(__file__).with_name('archive_worker.py'))])
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        process = subprocess.Popen(worker, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startup)
        try:
            from .process_tree import ProcessTree
            tree = ProcessTree(process)
            process.stdin.write((json.dumps(dict(argv=command, password=password, cwd=str(cwd) if cwd else None))+'\n').encode())
            process.stdin.flush()
        except Exception:
            process.kill()
            process.wait()
            if tree:tree.close()
            process.stdin.close()
            process.stdout.close()
            raise ValueError('Could not establish private archive password transport.') from None
    else:
        process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    chunks = queue.Queue(maxsize=8)
    stop = threading.Event()
    def read():
        try:
            while not stop.is_set():
                chunk = process.stdout.read(65536)
                while not stop.is_set():
                    try:
                        chunks.put(chunk, timeout=0.1)
                        break
                    except queue.Full:
                        pass
                if not chunk:
                    break
        except OSError:
            pass
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    collected = bytearray()
    try:
        try:
            if password is not None and os.name != "nt":
                process.stdin.write((password + "\n").encode("utf-8"))
                process.stdin.flush()
            if not private_console:
                process.stdin.close()
        except BrokenPipeError:
            pass
        while True:
            checkpoint(cancelled)
            try:
                data = chunks.get(timeout=0.1)
            except queue.Empty:
                if process.poll() is not None and not reader.is_alive():
                    break
                continue
            if not data:
                break
            if consume:
                consume(data)
            else:
                collected.extend(data)
                if len(collected) > 32 * 1024**2:
                    raise ValueError("7-Zip listing exceeds the supported preview limit.")
        while process.poll() is None:
            checkpoint(cancelled)
            stop.wait(0.05)
        if process.returncode:
            # Never persist tool output: prompts and diagnostic text can contain secrets.
            raise ValueError("7-Zip failed. Check the password, free space and source access; archive not saved.")
        return bytes(collected)
    finally:
        stop.set()
        if process.poll() is None:
            process.kill()
        process.wait()
        if tree:
            tree.close()
        reader.join(timeout=2)
        process.stdout.close()
        if not process.stdin.closed:
            process.stdin.close()


def remove_stage(store, item):
    if not item["stage"]:
        return
    saved = json.loads(item["stage"])
    root = Path(saved["path"])
    spec = json.loads(item["spec"])
    parent = Path(spec["parent"])
    if root.parent != parent or not root.name.startswith(f".mediatool-stage-{item['id']}-"):
        raise ValueError("Archive staging location is invalid; it was left untouched.")
    if os.path.lexists(root):
        info = root.lstat()
        if identity(info)[:2] != saved["identity"] or root.resolve().parent != parent:
            raise ValueError("Archive staging folder was replaced; it was left untouched.")
        pending = [root]
        while pending:
            folder = pending.pop()
            info = folder.lstat()
            if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Archive staging contains a link; it was left untouched.")
            with os.scandir(folder) as entries:
                for entry in entries:
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                        raise ValueError("Archive staging contains a link; it was left untouched.")
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(Path(entry.path))
                    elif not stat.S_ISREG(info.st_mode):
                        raise ValueError("Unexpected staging content; it was left untouched.")
        if identity(root.lstat())[:2] != saved["identity"]:
            raise ValueError("Archive staging ownership changed.")
        shutil.rmtree(root)
    current = store.item(item["id"])
    store.set_item(item["id"], current["state"], current["message"], stage=None)


def write_seven(spec, destination, cancelled, progress, hook, store, item):
    from .zip_archive import check_entry, CHUNK
    password = password_for(spec)
    # Plaintext source copies, intermediate archive and final owned temporary file.
    if shutil.disk_usage(spec["parent"]).free < 3 * spec["total_bytes"] + 16 * 1024**2:
        raise OSError("Not enough free space for archive staging (allow three times the source bytes plus 16 MiB).")
    root = Path(spec["parent"]) / f".mediatool-stage-{item['id']}-{uuid.uuid4().hex}"
    root.mkdir(mode=0o700)
    current = store.item(item["id"])
    store.set_item(item["id"], current["state"], "Preparing archive inputs", stage=json.dumps(
        dict(path=str(root), identity=identity(root.stat())[:2])))
    try:
        data = root / "data"
        data.mkdir(mode=0o700)
        digests, done = {}, 0
        for entry in spec["manifest"]["entries"]:
            checkpoint(cancelled)
            check_entry(entry, spec)
            target = data.joinpath(*entry["name"].rstrip("/").split("/"))
            if entry["folder"]:
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            with Path(entry["source"]).open("rb") as source, target.open("xb") as out:
                check_entry(entry, spec, source)
                while chunk := source.read(CHUNK):
                    checkpoint(cancelled)
                    out.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    progress(round(20 * done / max(1, spec["total_bytes"])), 100)
                    hook("chunk")
                check_entry(entry, spec, source)
            os.utime(target, ns=(entry["identity"][3], entry["identity"][3]))
            digests[entry["name"]] = digest.hexdigest()
            hook("entry_written")
        for entry in reversed(spec["manifest"]["entries"]):
            if entry["folder"]:
                target = data.joinpath(*entry["name"].rstrip("/").split("/"))
                os.utime(target, ns=(entry["modified_ns"], entry["modified_ns"]))
        kind = spec.get("archive_format", "zip")
        archive = root / ("output." + kind)
        args = ["a", "-t" + kind, "-mx=" + str(spec["level"]), "-mmt=2", "-bso0", "-bsp0", "-y"]
        if kind == "7z":
            args.extend(["-ms=off", "-md=16m"] if spec["level"] else ["-m0=Copy"])
        if password is not None:
            args.append("-p")
            args.extend(["-mhe=" + ("on" if spec["encrypt_names"] else "off")] if kind == "7z" else ["-mem=AES256"])
        seven_run([*args, "--", str(archive), "."], password, cancelled, cwd=data)
        with archive.open("rb") as source:
            while chunk := source.read(CHUNK):
                checkpoint(cancelled)
                destination.write(chunk)
        return digests
    finally:
        remove_stage(store, store.item(item["id"]))


def verify_seven(spec, temp, digests, cancelled, progress, hook):
    password = password_for(spec)
    switches = ["-p"] if password is not None else []
    listing = seven_run(["l", "-slt", "-ba", "-sccUTF-8", *switches, "--", str(temp)], password, cancelled)
    entries = []
    for block in listing.decode("utf-8-sig").replace("\r\n", "\n").strip().split("\n\n"):
        fields = dict(line.split(" = ", 1) for line in block.splitlines() if " = " in line)
        if "Path" in fields:
            entries.append(fields)
    expected = {e["name"].rstrip("/"): e for e in spec["manifest"]["entries"]}
    # 7-Zip materializes parent directories even when explicit folder entries are off.
    allowed_dirs = {str(Path(e["name"]).parent).replace("\\", "/") for e in spec["manifest"]["entries"]}
    for name in list(allowed_dirs):
        allowed_dirs.update(str(p).replace("\\", "/") for p in Path(name).parents)
    seen, files = set(), []
    for fields in entries:
        name = fields["Path"].replace("\\", "/").rstrip("/")
        folder = fields.get("Folder") == "+" or fields.get("Attributes", "").startswith("D")
        if folder and name not in expected and name in allowed_dirs:
            continue
        if name in seen or name not in expected or expected[name]["folder"] != folder:
            raise ValueError("Archive entries differ from the preview.")
        seen.add(name)
        if not folder:
            if int(fields["Size"]) != expected[name]["identity"][2] or (spec.get("encrypted") and int(fields["Size"]) > 0 and fields.get("Encrypted") != "+"):
                raise ValueError("Archive size or encryption verification failed.")
            if spec.get("encrypted") and int(fields["Size"]) > 0 and "AES" not in fields.get("Method", ""):
                raise ValueError("Archive does not use AES encryption.")
            files.append((name, int(fields["Size"])))
    if seen != set(expected):
        raise ValueError("Archive is missing planned entries.")
    if spec.get("encrypt_names"):
        try:
            seven_run(["l", "-slt", "-ba", "--", str(temp)], None, cancelled)
        except ValueError:
            pass
        else:
            raise ValueError("Archive filenames are readable without a password.")
    seven_run(["t", "-bso0", "-bsp0", *switches, "--", str(temp)], password, cancelled)
    index, offset, digest, done = 0, 0, hashlib.sha256(), 0
    def finish_empty():
        nonlocal index, offset, digest
        while index < len(files) and offset == files[index][1]:
            if digest.hexdigest() != digests[files[index][0]]:
                raise ValueError("Archive page/file checksum failed verification.")
            index += 1
            offset, digest = 0, hashlib.sha256()
    finish_empty()
    def consume(chunk):
        nonlocal index, offset, done
        while chunk:
            finish_empty()
            if index >= len(files):
                raise ValueError("Archive contains unexpected extracted bytes.")
            take = min(len(chunk), files[index][1] - offset)
            digest.update(chunk[:take])
            chunk = chunk[take:]
            offset += take
            done += take
        progress(55 + round(20 * done / max(1, spec["total_bytes"])), 100)
        hook("verify_chunk")
    seven_run(["x", "-so", "-bso0", "-bsp0", *switches, "--", str(temp)], password, cancelled, consume)
    finish_empty()
    if index != len(files):
        raise ValueError("Archive extraction was incomplete.")
