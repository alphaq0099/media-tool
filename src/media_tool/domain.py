"""Read-only input records and shared path/name rules. No Qt dependency."""
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import os
import re
import stat as stat_types


def natural_key(value: str):
    # Include the original text/path as a final tie-breaker when ordering records.
    return tuple((1, int(part)) if part.isdigit() else (0, part.casefold())
                 for part in re.split(r"(\d+)", value))


def path_key(path) -> str:
    return os.path.normcase(str(Path(os.path.abspath(path)).resolve(strict=False)))


@dataclass(frozen=True, slots=True)
class FileRecord:
    path: str
    name: str
    size: int
    kind: str
    sample: bool = False
    modified_ns: int | None = None
    device: int | None = None
    inode: int | None = None
    sort_key: tuple = field(init=False, repr=False, compare=False)
    search_name: str = field(init=False, repr=False, compare=False)
    extension: str = field(init=False, repr=False, compare=False)
    identity_key: str = field(init=False, repr=False, compare=False)
    modified_display: str = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        object.__setattr__(self, "sort_key",
                           (natural_key(self.name), self.path.casefold(), self.path))
        object.__setattr__(self, "search_name", self.name.casefold())
        object.__setattr__(self, "extension", "" if self.kind == "Folder" else Path(self.name).suffix.casefold())
        object.__setattr__(self, "identity_key", self.path if self.sample else path_key(self.path))
        stamp = ""
        if self.modified_ns is not None:
            try:
                stamp = datetime.fromtimestamp(self.modified_ns / 1e9).strftime("%Y-%m-%d %H:%M")
            except (ValueError, OverflowError, OSError):
                stamp = "Unknown"
        object.__setattr__(self, "modified_display", stamp)

    @classmethod
    def from_path(cls, path):
        path = Path(os.path.abspath(path))
        stat = path.stat()
        return cls(str(path), path.name, stat.st_size, "Folder" if stat_types.S_ISDIR(stat.st_mode) else category(path), False,
                   stat.st_mtime_ns, stat.st_dev, stat.st_ino)


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".avif", ".heic", ".heif", ".gif"}
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".wmv", ".ts"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".opus", ".wma"}
DOCUMENT_EXTS = {".txt", ".md", ".pdf", ".doc", ".docx", ".odt", ".xls", ".xlsx", ".ods", ".csv", ".ppt", ".pptx", ".odp", ".rtf"}
ARCHIVE_EXTS = {".zip", ".7z", ".rar", ".cbz", ".tar", ".gz", ".xz", ".bz2"}


def category(path: Path) -> str:
    suffix = path.suffix.casefold()
    for name, extensions in (("Image", IMAGE_EXTS), ("Video", VIDEO_EXTS),
                             ("Audio", AUDIO_EXTS), ("Document", DOCUMENT_EXTS),
                             ("Archive", ARCHIVE_EXTS)):
        if suffix in extensions:
            return name
    return "Other"


@dataclass(frozen=True, slots=True)
class FilterSpec:
    category: str = "All types"
    name: str = ""
    extensions: frozenset = frozenset()
    allowed_extensions: frozenset | None = None

    @classmethod
    def parse(cls, category="All types", name="", extensions="", *, allowed_extensions=None):
        tokens = re.split(r"[,;\s]+", extensions.strip().casefold())
        parsed = frozenset("." + token.removeprefix("*.").lstrip(".")
                           for token in tokens if token and token not in ("*", "*.*"))
        return cls(category, name.casefold(), parsed, allowed_extensions)

    def matches(self, item: FileRecord) -> bool:
        return ((self.category == "All types" or self.category == item.kind)
                and (self.allowed_extensions is None or item.extension in self.allowed_extensions)
                and (not self.name or self.name in item.search_name)
                and (not self.extensions or item.extension in self.extensions))


def format_size(size: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:,.0f} {unit}" if unit == "B" else f"{size:,.1f} {unit}"
        size /= 1024
