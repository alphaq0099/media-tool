"""Read-only ZIP manifests. Roots, relative names and discovery are frozen together."""
import fnmatch
import os
from pathlib import Path, PurePosixPath
import stat
import unicodedata

from .domain import natural_key, path_key
from .jobs import identity
from .planning import OperationPlan, PlanItem, PlanCancelled, validate_name

MAX_ENTRIES = 100_000


def checked(path, directory=False):
    path = Path(path)
    info = path.lstat()
    valid = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not valid or getattr(info, "st_file_attributes", 0) & 0x400 or not info.st_ino:
        raise ValueError(f"Expected an ordinary {'folder' if directory else 'file'} with stable identities; links are not archived: {path}")
    return info


def name_key(name):
    return unicodedata.normalize("NFC", name).casefold()


def safe_name(name):
    path = PurePosixPath(name.rstrip("/"))
    if not name or path.is_absolute() or "\\" in name or any(part in ("", ".", "..") for part in name.rstrip("/").split("/")):
        raise ValueError(f"Unsafe ZIP entry name: {name}")
    for part in path.parts:
        error = validate_name(part)
        if error:
            raise ValueError(f"ZIP entry {name}: {error}")
    if len(name.encode("utf-8")) > 65535:
        raise ValueError("ZIP entry name exceeds the ZIP format limit.")
    return name


def patterns(text):
    values = tuple(p.strip().replace("\\", "/") for p in text.split(";") if p.strip())
    if any(p.startswith("/") or ".." in p.split("/") or ":" in p for p in values):
        raise ValueError("Exclusions must be relative patterns, such as *.tmp; cache; build/*.")
    return values


def omitted(path, relative, options):
    parts = relative.split("/")
    for pattern in options["patterns"]:
        pattern = pattern.casefold().rstrip("/")
        if any(fnmatch.fnmatchcase("/".join(parts[index:]).casefold(), pattern) for index in range(len(parts))) or ("/" not in pattern and any(fnmatch.fnmatchcase(part.casefold(), pattern) for part in parts)):
            return True
    info = path.lstat()
    include_hidden = options.get("hidden_folders", options["hidden"]) if stat.S_ISDIR(info.st_mode) else options["hidden"]
    return not include_hidden and (path.name.startswith(".") or bool(getattr(info, "st_file_attributes", 0) & 2))


def discover(roots, options, ignored, cancelled=lambda: False, preparing=False):
    entries, directories, excluded = [], {}, []
    names = set()
    visited = 0
    ignored = set(ignored)
    destination = Path(options["destination"]) if options["destination"] else None
    def check_cancel():
        if cancelled():
            if preparing:
                raise PlanCancelled()
            from .jobs import checkpoint
            checkpoint(cancelled)
    def directory(path):
        info = checked(path, True)
        directories[str(path)] = identity(info)[:2]
    for ordinal, root in enumerate(roots):
        check_cancel()
        source = Path(root["path"])
        info = checked(source, root["folder"])
        expected = identity(info)[:2] if root["folder"] else identity(info)
        if expected != root["identity"]:
            raise ValueError(f"Archive input changed since discovery: {source}")
        directory(source.parent)
        stack = [(source, root["alias"])]
        while stack:
            check_cancel()
            path, relative = stack.pop()
            visited += 1
            if visited > MAX_ENTRIES:
                raise ValueError(f"ZIP discovery exceeds {MAX_ENTRIES:,} paths. Split the selection.")
            # Exclusions are deliberate omissions; skipped discovery errors are not.
            if str(path) in ignored or (destination and path == destination and path != source):
                excluded.append(relative)
                continue
            if omitted(path, relative, options):
                excluded.append(relative)
                continue
            info = path.lstat()
            folder = stat.S_ISDIR(info.st_mode)
            checked(path, folder)
            name = safe_name(relative + ("/" if folder else ""))
            key = name_key(name.rstrip("/"))
            if key in names:
                raise ValueError(f"ZIP entries collide on a case-insensitive filesystem: {name}")
            names.add(key)
            if folder:
                directory(path)
                with os.scandir(path) as scan:
                    children = []
                    for child in scan:
                        check_cancel()
                        children.append(child)
                        if len(children) > MAX_ENTRIES:
                            raise ValueError(f"Folder exceeds {MAX_ENTRIES:,} children. Split the selection.")
                    children.sort(key=lambda p: (natural_key(p.name), p.name))
                if options["empty"]:
                    entries.append(dict(source=str(path), name=name, folder=True, identity=identity(info)[:2], modified_ns=info.st_mtime_ns, root=ordinal))
                stack.extend((Path(child.path), f"{relative}/{child.name}") for child in reversed(children))
            else:
                if options.get("comic") and path.suffix.casefold() not in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"):
                    excluded.append(relative)
                    continue
                entries.append(dict(source=str(path), name=name, folder=False, identity=identity(info), root=ordinal))
            if len(names) > MAX_ENTRIES:
                raise ValueError(f"ZIP discovery exceeds {MAX_ENTRIES:,} entries. Split the selection.")
    return dict(entries=entries, directories=directories, excluded=excluded)


def plan_archives(records, request, cancelled=lambda: False):
    if request.archive_format not in ("zip", "cbz", "7z"):
        raise ValueError("Choose ZIP, CBZ or 7z.")
    if request.archive_format == "cbz" and request.archive_encrypt:
        raise ValueError("CBZ does not support encryption in comic readers. Choose ZIP or 7z.")
    if request.archive_format == "7z" or request.archive_encrypt:
        from .tool_paths import resolve_tool
        if not resolve_tool("7z"):
            raise ValueError("7-Zip is required for 7z and encrypted ZIP archives. Install 7-Zip or choose an unencrypted ZIP/CBZ.")
    if request.archive_mode not in ("combined", "separate") or request.archive_level not in (0, 1, 6, 9):
        raise ValueError("Choose a ZIP batch mode and supported compression level.")
    if request.collision not in ("skip", "unique"):
        raise ValueError("Choose Skip or Use a unique name for conflicts.")
    destination = None
    if request.destination.strip():
        destination = Path(request.destination).expanduser()
        if not destination.is_absolute():
            raise ValueError("Choose an absolute output folder.")
        destination = destination.resolve(strict=True)
        checked(destination, True)
    options = dict(patterns=patterns(request.archive_exclusions), hidden=request.archive_hidden,
                   empty=request.archive_empty_dirs, destination=str(destination) if destination else "")
    if request.archive_hidden_folders is not None:
        options["hidden_folders"] = request.archive_hidden_folders
    if request.archive_format == "cbz":
        options.update(comic=True, empty=False)
    ordered, seen, skipped = [], set(), []
    for record in sorted(records, key=lambda r: r.sort_key):
        if cancelled():
            raise PlanCancelled()
        if record.sample:
            raise ValueError("ZIP creation needs real files or folders, not sample rows.")
        key = path_key(record.path)
        if key in seen:
            skipped.append(PlanItem(record, None, "Skipped", "Duplicate input path."))
            continue
        seen.add(key)
        ordered.append(record)
    if not ordered:
        return OperationPlan(request, tuple(skipped))
    groups = [ordered] if request.archive_mode == "combined" else [[r] for r in ordered]
    output_names = set(seen)
    planned = []
    for group in groups:
        source = group[0]
        raw_name = request.archive_name.strip() if request.archive_mode == "combined" else (source.name if source.kind == "Folder" else Path(source.name).stem)
        suffix = "." + request.archive_format
        base = raw_name[:-len(suffix)] if raw_name.casefold().endswith(suffix) else raw_name
        name = base + suffix
        error = validate_name(name)
        if not base or error:
            raise ValueError(error or "Enter a ZIP filename.")
        parent = destination or Path(source.path).parent.resolve(strict=True)
        checked(parent, True)
        output = parent / name
        conflict = path_key(output) in output_names or os.path.lexists(output)
        if conflict and request.collision == "skip":
            planned.append(PlanItem(source, str(output), "Skipped", "ZIP output already exists or is reserved."))
            continue
        counter = 1
        while path_key(output) in output_names or os.path.lexists(output):
            if cancelled():
                raise PlanCancelled()
            output = parent / f"{base}_{counter}{suffix}"
            if validate_name(output.name) or counter > 100000:
                raise ValueError("Cannot reserve a unique ZIP filename. Choose a shorter name or another folder.")
            counter += 1
        output_names.add(path_key(output))
        planned.append((group, output))
    ignored = [str(p[1]) for p in planned if isinstance(p, tuple)]
    items = list(skipped)
    total_entries = 0
    for candidate in planned:
        if isinstance(candidate, PlanItem):
            items.append(candidate)
            continue
        group, output = candidate
        source = group[0]
        try:
            roots, inputs, aliases = [], [], set()
            # In a combined ZIP a selected descendant is already covered by its folder.
            folders = [Path(r.path).resolve() for r in group if r.kind == "Folder"]
            for record in group:
                path = Path(record.path)
                info = checked(path, record.kind == "Folder")
                frozen = [record.device, record.inode, record.size, record.modified_ns]
                if identity(info) != frozen:
                    raise ValueError(f"Input changed since discovery: {path}")
                canonical = path.resolve(strict=True)
                inputs.append(dict(source=record.path, identity=frozen))
                if any(parent != canonical and parent in canonical.parents for parent in folders):
                    continue
                alias = record.name
                suffix = Path(alias).suffix if record.kind != "Folder" else ""
                stem = alias[:-len(suffix)] if suffix else alias
                number = 2
                while name_key(alias) in aliases:
                    alias = f"{stem} ({number}){suffix}"
                    number += 1
                safe_name(alias)
                aliases.add(name_key(alias))
                roots.append(dict(path=str(canonical), folder=record.kind == "Folder", alias=alias,
                                  identity=identity(info)[:2] if record.kind == "Folder" else identity(info)))
            manifest = discover(roots, options, ignored, cancelled, preparing=True)
            source_manifest = manifest
            if request.archive_format == "cbz":
                from .comic_archive import comic_manifest
                manifest = comic_manifest(manifest, request.archive_number_pages, cancelled)
            total_entries += len(manifest["entries"]) + len(manifest["directories"])
            if total_entries > MAX_ENTRIES:
                raise ValueError(f"Batch manifest exceeds {MAX_ENTRIES:,} entries/folders. Split the selection.")
            if not manifest["entries"]:
                items.append(PlanItem(source, str(output), "Skipped", "No included entries; check exclusions and empty-folder settings."))
                continue
            count = sum(not e["folder"] for e in manifest["entries"])
            size = sum(e["identity"][2] for e in manifest["entries"] if not e["folder"])
            spec = dict(name=f"Selection ({len(inputs)} inputs)" if len(inputs) > 1 else source.name,
                        source=source.path, identity=[source.device, source.inode, source.size, source.modified_ns],
                        inputs=inputs, roots=roots, options=options, ignored=ignored, manifest=manifest,
                        output=str(output), parent=str(output.parent), parent_identity=identity(output.parent.stat())[:2],
                        level=request.archive_level, total_bytes=size, file_count=count)
            spec.update(archive_format=request.archive_format, encrypted=request.archive_encrypt,
                        encrypt_names=request.archive_encrypt_names if request.archive_format == "7z" and request.archive_encrypt else False)
            if request.archive_format == "cbz":
                spec["source_manifest"] = source_manifest
            reason = (f"{len(inputs)} input(s) → {request.archive_format.upper()}; {count:,} files, {len(manifest['entries'])-count:,} folder entries, {size:,} source bytes. "
                      f"{len(manifest['excluded'])} exclusions.")
            items.append(PlanItem(source, str(output), "Planned", reason, archive_spec=spec))
        except PlanCancelled:
            raise
        except (OSError, ValueError, RuntimeError) as error:
            items.append(PlanItem(source, str(output), "Invalid", f"Cannot create {request.archive_format.upper()}: {error}"))
    return OperationPlan(request, tuple(items))


def archive_snapshot(plan):
    if plan.request.operation != "archives" or any(i.status not in ("Planned", "Skipped") for i in plan.items):
        raise ValueError("Resolve invalid archive inputs and preview again. Nothing was archived.")
    specs = []
    for item in plan.items:
        if item.status == "Planned":
            specs.append(item.archive_spec)
        else:
            specs.append(dict(name=item.source.name, source=item.source.path,
                identity=[item.source.device, item.source.inode, item.source.size, item.source.modified_ns],
                output=item.output, initial_state="skipped", initial_message=item.reason))
    return tuple(specs)
