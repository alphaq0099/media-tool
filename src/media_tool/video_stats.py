"""Journaled, exclusively created two-pass files; cleanup checks identities."""
import json
import os
from pathlib import Path
import stat
import uuid

from .jobs import identity, regular

NAMES = ("stats-0.log", "stats-0.log.temp", "stats-0.log.mbtree", "stats-0.log.mbtree.temp")
HEVC_NAMES = ("x265.log", "x265.log.temp", "x265.log.cutree", "x265.log.cutree.temp")


def scope_names(scope):
    codec = scope.get("codec", "h264")
    if codec not in ("h264", "hevc"):
        raise ValueError("Unknown two-pass file scope.")
    return HEVC_NAMES if codec == "hevc" else NAMES


def directory_identity(path):
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("Two-pass directory was replaced; it was left untouched.")
    return identity(info)[:2]


def check_scope(store, scope):
    root = Path(scope["path"])
    if root.parent.resolve() != store.directory.resolve() or directory_identity(root) != scope["identity"]:
        raise ValueError("Two-pass directory ownership changed; it was left untouched.")
    owned = scope["files"]
    for path in root.iterdir():
        if path.name not in scope_names(scope) or identity(regular(path))[:2] not in owned:
            raise ValueError("Two-pass files were replaced or unrelated files appeared; they were left untouched.")
    return root


def create_scope(store, item_id, codec="h264"):
    root = store.directory.resolve() / f".mediatool-pass-{uuid.uuid4().hex}"
    root.mkdir(mode=0o700)
    scope = dict(path=str(root), identity=directory_identity(root), files=[], codec=codec)
    def save():
        row = store.item(item_id)
        store.set_item(item_id, row["state"], row["message"], stage=json.dumps(scope))
    save()
    for name in scope_names(scope):
        with (root / name).open("xb") as handle:
            scope["files"].append(identity(os.fstat(handle.fileno()))[:2])
        save()
    return scope


def cleanup_scope(store, item_id):
    row = store.item(item_id)
    if not row["stage"]:
        return
    scope = json.loads(row["stage"])
    root = Path(scope["path"])
    if os.path.lexists(root):
        check_scope(store, scope)
        for path in root.iterdir():
            # Check again at the mutation boundary; never recurse or glob-delete.
            if identity(regular(path))[:2] not in scope["files"]:
                raise ValueError("Two-pass file ownership changed; file was kept.")
            path.unlink()
        if directory_identity(root) != scope["identity"]:
            raise ValueError("Two-pass directory changed; directory was kept.")
        root.rmdir()
    store.set_item(item_id, row["state"], row["message"], stage=None)
