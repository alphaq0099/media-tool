"""Read-only folder snapshots and cached, bottom-up inspector views. No Qt."""
from dataclasses import dataclass, replace
import os
from pathlib import Path
import stat

from .domain import natural_key
from .jobs import Cancelled, checkpoint


def linked(path, info=None):
    info = info or Path(path).lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def directory_identity(path):
    """Reject redirected ancestors as well as redirected final components."""
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("Choose an absolute folder path.")
    for part in (path, *path.parents):
        info = part.lstat()
        if linked(part, info) or not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"Links, junctions and redirected folders are not supported: {part}")
    if not info.st_ino:
        raise ValueError("This filesystem does not provide stable folder identities.")
    own = path.lstat()
    if not own.st_ino:
        raise ValueError("This filesystem does not provide stable folder identities.")
    return [own.st_dev, own.st_ino]


def entry_identity(info, kind):
    return ([info.st_dev, info.st_ino] if kind == "Folder" else
            [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns])


@dataclass(frozen=True, slots=True)
class FolderNode:
    path: str
    name: str
    parent: int
    depth: int
    kind: str
    identity: tuple
    size: int = 0
    total: int = 0
    files: int = 0
    partial: bool = False
    issue: str = ""


@dataclass(frozen=True, slots=True)
class FolderSnapshot:
    root: str
    nodes: tuple
    children: tuple
    issues: tuple = ()
    cancelled: bool = False

    @property
    def complete(self):
        return bool(self.nodes) and not self.cancelled and not self.nodes[0].partial


def aggregate(root, nodes, children, issues=(), cancelled=False):
    """Each edge is visited once, regardless of tree depth."""
    nodes = list(nodes)
    for index in range(len(nodes) - 1, -1, -1):
        node = nodes[index]
        if node.kind == "Folder":
            nodes[index] = replace(node,
                total=sum(nodes[c].total for c in children[index]),
                files=sum(nodes[c].files for c in children[index]),
                partial=node.partial or any(nodes[c].partial for c in children[index]))
    return FolderSnapshot(str(root), tuple(nodes), tuple(tuple(c) for c in children),
                          tuple(issues), cancelled)


def scan_folder(root, cancelled=lambda: False, progress=lambda count: None):
    """Full scan for accurate totals; UI rows are materialized lazily afterwards.

    Links and foreign filesystems are visible but never traversed. Totals count
    logical file bytes per entry (hard links count per name), not allocated space.
    """
    root = Path(os.path.abspath(root))
    root_id = directory_identity(root)
    nodes = [FolderNode(str(root), root.name or str(root), -1, 0, "Folder", tuple(root_id))]
    children, issues, stack = [[]], [], [0]
    directory_stamps = {}
    visited = {tuple(root_id)}
    stopped = False
    while stack:
        index = stack.pop()
        node = nodes[index]
        path = Path(node.path)
        try:
            checkpoint(cancelled)
            if directory_identity(path) != list(node.identity):
                raise ValueError("Folder changed during discovery.")
            before = path.lstat()
            # A directory is enumerated once. Sorting happens in the cached view.
            with os.scandir(path) as entries:
                for entry in entries:
                    checkpoint(cancelled)
                    full = path / entry.name
                    try:
                        # Windows DirEntry.stat caches discovery data with zero
                        # inode/device values; lstat supplies durable identities.
                        info = full.lstat()
                        issue, partial = "", False
                        if linked(full, info):
                            kind, issue = "Link", "Link / junction excluded; target was not scanned."
                        elif info.st_dev != root_id[0]:
                            kind, issue, partial = "Excluded", "Different filesystem excluded.", True
                        elif stat.S_ISDIR(info.st_mode):
                            kind = "Folder"
                            if (info.st_dev, info.st_ino) in visited or not info.st_ino:
                                kind, issue, partial = "Excluded", "Repeated or unstable folder identity excluded.", True
                            else:
                                visited.add((info.st_dev, info.st_ino))
                        elif stat.S_ISREG(info.st_mode) and info.st_ino:
                            kind = "File"
                        else:
                            kind, issue, partial = "Excluded", "Unsupported filesystem entry excluded.", True
                        identity = entry_identity(info, kind)
                        size = info.st_size if kind == "File" else 0
                    except OSError as error:
                        kind, identity, size = "Excluded", (), 0
                        issue, partial = str(error), True
                    child = len(nodes)
                    nodes.append(FolderNode(str(full), entry.name, index, node.depth + 1,
                                            kind, tuple(identity), size, size,
                                            int(kind == "File"), partial, issue))
                    children.append([])
                    children[index].append(child)
                    if kind == "Folder":
                        stack.append(child)
                    if issue:
                        issues.append(f"{full}: {issue}")
                    if child % 200 == 0:
                        progress(child)
            after = path.lstat()
            if entry_identity(after, "Folder") != list(node.identity) or before.st_mtime_ns != after.st_mtime_ns:
                raise ValueError("Folder contents changed during discovery. Refresh to obtain complete totals.")
            directory_stamps[index] = after.st_mtime_ns
        except Cancelled:
            stopped = True
            nodes[0] = replace(nodes[0], partial=True, issue="Scan cancelled; totals are partial.")
            issues.append("Scan cancelled; unvisited entries are not included in totals.")
            break
        except (OSError, ValueError) as error:
            nodes[index] = replace(nodes[index], partial=True, issue=str(error))
            issues.append(f"{path}: {error}")
    # Detect entries changing while later subtrees were scanned. This remains a
    # snapshot, not a filesystem lock; execution independently rechecks its plan.
    if not stopped:
        for index, node in enumerate(nodes):
            try:
                checkpoint(cancelled)
                if node.kind not in ("File", "Folder") or node.partial:
                    continue
                info = Path(node.path).lstat()
                if (linked(node.path, info) or entry_identity(info, node.kind) != list(node.identity)
                        or (node.kind == "Folder" and info.st_mtime_ns != directory_stamps.get(index))):
                    raise ValueError("Entry changed before the scan finished. Refresh for complete totals.")
            except Cancelled:
                stopped = True
                nodes[0] = replace(nodes[0], partial=True, issue="Scan cancelled; totals are partial.")
                issues.append("Scan cancelled during final snapshot checks; totals are partial.")
                break
            except (OSError, ValueError) as error:
                nodes[index] = replace(node, partial=True, issue=str(error))
                issues.append(f"{node.path}: {error}")
    progress(len(nodes) - 1)
    return aggregate(root, nodes, children, issues, stopped)


@dataclass(frozen=True, slots=True)
class FolderView:
    snapshot: FolderSnapshot
    children: tuple
    positions: tuple
    matching_bytes: tuple
    matching_files: tuple
    min_bytes: int = 0
    max_bytes: int | None = None


def folder_view(snapshot, minimum=0, maximum=None, directories_only=False,
                depth=0, sort="name", cancelled=lambda: False, descending=None):
    if minimum < 0 or (maximum is not None and maximum < minimum):
        raise ValueError("Maximum size must be at least the minimum, or zero for no limit.")
    if sort not in ("name", "size", "matching", "files", "depth", "status"):
        raise ValueError("Choose a supported folder sort.")
    if descending is None:
        descending = sort in ("size", "matching", "files")
    nodes, count = snapshot.nodes, len(snapshot.nodes)
    sizes, files, visible = [0] * count, [0] * count, [()] * count
    positions = [0] * count
    for index in range(count - 1, -1, -1):
        checkpoint(cancelled)
        node = nodes[index]
        if node.kind == "File":
            matches = minimum <= node.size and (maximum is None or node.size <= maximum)
            sizes[index], files[index] = (node.size, 1) if matches else (0, 0)
        elif node.kind == "Folder":
            sizes[index] = sum(sizes[c] for c in snapshot.children[index])
            files[index] = sum(files[c] for c in snapshot.children[index])
        chosen = [c for c in snapshot.children[index]
                  if (not depth or nodes[c].depth <= depth)
                  and (not directories_only or nodes[c].kind == "Folder")
                  and (nodes[c].kind != "File" or files[c])]
        chosen.sort(key=lambda c: (natural_key(nodes[c].name), nodes[c].name),
                    reverse=bool(descending) if sort == "name" else False)
        if sort == "name":
            chosen.sort(key=lambda c: nodes[c].kind != "Folder")
        else:
            def measure(c):
                if sort == "size":
                    return nodes[c].total
                if sort == "matching":
                    return sizes[c]
                if sort == "files":
                    return files[c]
                if sort == "depth":
                    return nodes[c].depth
                return "Partial" if nodes[c].partial else "Excluded" if nodes[c].kind in ("Link", "Excluded") else nodes[c].kind
            chosen.sort(key=measure, reverse=bool(descending))
        visible[index] = tuple(chosen)
        for position, child in enumerate(chosen):
            positions[child] = position
    return FolderView(snapshot, tuple(visible), tuple(positions), tuple(sizes), tuple(files), minimum, maximum)


def subtree_manifest(snapshot, index, cancelled=lambda: False):
    root = Path(snapshot.nodes[index].path)
    members, pending = [], [index]
    while pending:
        checkpoint(cancelled)
        current = pending.pop()
        node = snapshot.nodes[current]
        if node.partial or node.kind not in ("File", "Folder"):
            raise ValueError("Subtree contains an unreadable, unsupported or linked entry; left in place.")
        members.append({"relative": Path(node.path).relative_to(root).as_posix(),
                        "kind": node.kind, "identity": list(node.identity)})
        pending.extend(snapshot.children[current])
    return tuple(members)
