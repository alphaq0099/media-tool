"""Streaming ZIP creation, full entry verification and protected final commit."""
from datetime import datetime
import hashlib
import json
import os
import shutil
from pathlib import Path
import stat
import uuid
import zipfile

from .jobs import (checkpoint, identity, regular, owns_temp, cleanup_temp, hash_file,
                   commit_no_replace)
from .zip_plan import checked, discover, safe_name

CHUNK = 1024 * 1024


def zip_time(modified_ns):
    try:
        stamp = datetime.fromtimestamp(modified_ns / 1_000_000_000)
    except (OverflowError, OSError, ValueError):
        stamp = datetime(1980, 1, 1) if modified_ns < 0 else datetime(2107, 12, 31, 23, 59, 58)
    if stamp.year < 1980:
        stamp = datetime(1980, 1, 1)
    elif stamp.year > 2107:
        stamp = datetime(2107, 12, 31, 23, 59, 58)
    return (stamp.year, stamp.month, stamp.day, stamp.hour, stamp.minute, stamp.second // 2 * 2)


def check_parent(spec):
    parent = Path(spec["parent"])
    if parent.resolve(strict=True) != parent or identity(checked(parent, True))[:2] != spec["parent_identity"] or Path(spec["output"]).parent != parent:
        raise ValueError("ZIP output folder changed. Preview again.")
    return parent


def check_entry(entry, spec, handle=None):
    root = spec["roots"][entry["root"]]
    anchor = Path(root["path"]).parent
    parent = Path(entry["source"]).parent
    while True:
        if identity(checked(parent, True))[:2] != spec["manifest"]["directories"].get(str(parent)):
            raise ValueError(f"Source folder changed while archiving: {parent}")
        if parent == anchor:
            break
        if parent == parent.parent or anchor not in parent.parents:
            raise ValueError("ZIP source escaped its planned root.")
        parent = parent.parent
    info = checked(entry["source"], entry["folder"])
    actual = identity(info)[:2] if entry["folder"] else identity(info)
    if actual != entry["identity"] or (handle is not None and identity(os.fstat(handle.fileno())) != entry["identity"]):
        raise ValueError(f"Source changed while archiving: {entry['source']}")


def check_manifest(spec, temp, cancelled):
    current = discover(spec["roots"], spec["options"], [*spec["ignored"], str(temp)], cancelled)
    # Excluded names can change freely; included membership, paths and identities cannot.
    def comparable(entries):
        return [{k: v for k, v in entry.items() if k != "modified_ns"} for entry in entries]
    expected = spec.get("source_manifest", spec["manifest"])
    if comparable(current["entries"]) != comparable(expected["entries"]) or current["directories"] != expected["directories"]:
        raise ValueError("Folder contents changed since ZIP preview. Add changed inputs again and preview.")


def write_zip(spec, destination, cancelled, progress, hook):
    digests, done = {}, 0
    entries = spec["manifest"]["entries"]
    method = zipfile.ZIP_STORED if spec["level"] == 0 else zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(destination, "w", compression=method, compresslevel=spec["level"] or None, allowZip64=True) as archive:
        for entry in entries:
            checkpoint(cancelled)
            check_entry(entry, spec)
            name = safe_name(entry["name"])
            info = zipfile.ZipInfo(name, zip_time(entry["modified_ns"] if entry["folder"] else entry["identity"][3]))
            info.create_system = 3
            info.external_attr = ((stat.S_IFDIR | 0o755) << 16 | 0x10) if entry["folder"] else ((stat.S_IFREG | 0o644) << 16)
            info.compress_type = zipfile.ZIP_STORED if entry["folder"] else method
            info.compress_level = spec["level"] or None
            info.file_size = 0 if entry["folder"] else entry["identity"][2]
            if entry["folder"]:
                archive.writestr(info, b"")
                hook("entry_written")
                continue
            digest, size = hashlib.sha256(), 0
            with Path(entry["source"]).open("rb") as source:
                check_entry(entry, spec, source)
                with archive.open(info, "w", force_zip64=info.file_size >= zipfile.ZIP64_LIMIT) as target:
                    while chunk := source.read(CHUNK):
                        checkpoint(cancelled)
                        target.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                        done += len(chunk)
                        progress(round(55 * done / max(1, spec["total_bytes"])), 100)
                        hook("chunk")
                check_entry(entry, spec, source)
            if size != entry["identity"][2]:
                raise ValueError("ZIP source size changed while reading.")
            digests[name] = digest.hexdigest()
            hook("entry_written")
    return digests


def verify_zip(spec, temp, digests, cancelled, progress, hook):
    entries = spec["manifest"]["entries"]
    done = 0
    with zipfile.ZipFile(temp, "r") as archive:
        infos = archive.infolist()
        if [i.filename for i in infos] != [e["name"] for e in entries] or archive.comment:
            raise ValueError("ZIP contents do not match the planned manifest.")
        for entry, info in zip(entries, infos):
            checkpoint(cancelled)
            expected_size = 0 if entry["folder"] else entry["identity"][2]
            method = zipfile.ZIP_STORED if entry["folder"] or spec["level"] == 0 else zipfile.ZIP_DEFLATED
            expected_time = zip_time(entry["modified_ns"] if entry["folder"] else entry["identity"][3])
            if info.is_dir() != entry["folder"] or info.file_size != expected_size or info.compress_type != method or info.flag_bits & 1 or info.date_time != expected_time:
                raise ValueError(f"ZIP entry properties failed verification: {info.filename}")
            digest, size = hashlib.sha256(), 0
            with archive.open(info) as source:
                while chunk := source.read(CHUNK):
                    checkpoint(cancelled)
                    digest.update(chunk)
                    size += len(chunk)
                    done += len(chunk)
                    progress(55 + round(20 * done / max(1, spec["total_bytes"])), 100)
                    hook("verify_chunk")
            if size != expected_size or (not entry["folder"] and digest.hexdigest() != digests[entry["name"]]):
                raise ValueError(f"ZIP entry checksum failed verification: {info.filename}")
            if not entry["folder"]:
                check_entry(entry, spec)
            hook("entry_verified")


def run_archive(store, item, cancelled, progress, hook=lambda point: None):
    spec = json.loads(item["spec"])
    kind = spec.get("archive_format", "zip").upper()
    external = spec.get("archive_format") == "7z" or spec.get("encrypted", False)
    if item["stage"]:
        from .seven_archive import remove_stage
        remove_stage(store, item)
    parent = check_parent(spec)
    output = Path(spec["output"])
    if item["digest"] and owns_temp(item, output):
        if hash_file(output, cancelled) != item["digest"]:
            raise ValueError("Previously committed ZIP changed; it was left untouched.")
        cleanup_temp(store, item["id"])
        return f"Recovered verified {kind}. Originals kept."
    if item["temp"] and os.path.lexists(item["temp"]) and not cleanup_temp(store, item["id"]):
        raise ValueError("Saved ZIP temporary file was replaced; it was kept.")
    checkpoint(cancelled)
    if os.path.lexists(output):
        raise FileExistsError("ZIP output appeared after preview; existing file kept.")
    if spec["level"] == 0:
        needed = spec["total_bytes"] + sum(200 + 2 * len(e["name"].encode("utf-8")) for e in spec["manifest"]["entries"]) + 1024**2
        if shutil.disk_usage(parent).free < needed:
            raise OSError(f"Not enough free space for a Store ZIP: allow at least {needed:,} bytes, including overhead.")
    temp = parent / f".mediatool-{item['job_id']}-{item['id']}-{uuid.uuid4().hex}.part"
    check_manifest(spec, temp, cancelled)
    with temp.open("xb") as destination:
        info = os.fstat(destination.fileno())
        store.set_item(item["id"], "running", "Creating archive; originals kept", temp=str(temp),
                       temp_dev=str(info.st_dev), temp_ino=str(info.st_ino), digest=None)
        hook("created")
        checkpoint(cancelled)
        if external:
            from .seven_archive import write_seven
            digests = write_seven(spec, destination, cancelled, progress, hook, store, item)
        else:
            digests = write_zip(spec, destination, cancelled, progress, hook)
        destination.flush()
        os.fsync(destination.fileno())
    hook("written")
    if not owns_temp(store.item(item["id"])):
        raise ValueError("ZIP temporary file ownership changed.")
    store.set_item(item["id"], "verifying", "Reading every archive file and checking original hashes")
    if external:
        from .seven_archive import verify_seven
        verify_seven(spec, temp, digests, cancelled, progress, hook)
    else:
        verify_zip(spec, temp, digests, cancelled, progress, hook)
    check_manifest(spec, temp, cancelled)
    digest = hash_file(temp, cancelled, lambda done, total: progress(75 + round(5 * done / max(1, total)), 100))
    verified_identity = identity(regular(temp))
    store.set_item(item["id"], "committing", "Checking originals again before archive commit", digest=digest,
                   source_digest=json.dumps({e["source"]: digests[e["name"]] for e in spec["manifest"]["entries"] if not e["folder"]}))
    hook("prepared")
    checkpoint(cancelled)
    check_parent(spec)
    check_manifest(spec, temp, cancelled)
    checked_bytes = 0
    for entry in spec["manifest"]["entries"]:
        checkpoint(cancelled)
        check_entry(entry, spec)
        if not entry["folder"]:
            if hash_file(entry["source"], cancelled,
                    lambda done, total: progress(80 + round(15 * (checked_bytes + done) / max(1, spec["total_bytes"])), 100)) != digests[entry["name"]]:
                raise ValueError(f"Original contents changed during ZIP creation: {entry['source']}")
            checked_bytes += entry["identity"][2]
            check_entry(entry, spec)
    if hash_file(temp, cancelled, lambda done, total: progress(95 + round(4 * done / max(1, total)), 100)) != digest:
        raise ValueError("ZIP temporary contents changed after verification.")
    check_parent(spec)
    check_manifest(spec, temp, cancelled)
    if identity(regular(temp)) != verified_identity or not owns_temp(store.item(item["id"])):
        raise ValueError("ZIP temporary file changed after verification.")
    commit_no_replace(temp, output)
    hook("committed")
    progress(100, 100)
    return f"{kind} verified: {spec['file_count']:,} files, {spec['total_bytes']:,} source bytes → {output.stat().st_size:,} archive bytes. Originals kept."
