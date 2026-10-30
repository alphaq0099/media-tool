"""Deterministic output previews. These functions never create or modify files."""
from dataclasses import dataclass
from pathlib import Path
from collections import Counter
import os
import re
import stat

from .domain import FileRecord, path_key


@dataclass(frozen=True, slots=True)
class PlanRequest:
    operation: str
    destination: str = ""
    extension: str = "jpg"
    collision: str = "unique"
    prefix: str = "File_"
    start: int = 1
    padding: int = 4
    quality: str = "Balanced"
    rename_style: str = "sequence"
    separator: str = " "
    pattern: str = ""
    seed: str = ""
    image_quality: int = 85
    alpha_background: str = "#ffffff"
    metadata: str = "remove"
    resize_mode: str = "none"
    resize_width: int = 1920
    resize_height: int = 1080
    resize_percent: int = 50
    resize_aspect: bool = True
    resize_no_upscale: bool = True
    video_crf: int = 23
    video_preset: str = "veryfast"
    video_threads: int = 2
    video_audio: int = 1
    video_allow_omissions: bool = False
    video_mode: str = "quality"
    video_target_size: float = 10.0
    video_target_unit: str = "MB"
    video_audio_kbps: int = 128
    video_codec: str = "h264"
    video_audio_mode: str = "single"
    video_audio_tracks: str = ""
    video_subtitle_mode: str = "none"
    video_subtitle_tracks: str = ""
    video_external_enabled: bool = False
    video_external_subtitle: str = ""
    video_action: str = "convert"
    keep_originals: bool = True
    video_reuse_analysis: bool = False
    archive_mode: str = "combined"
    archive_name: str = "Archive"
    archive_level: int = 6
    archive_exclusions: str = ""
    archive_hidden: bool = True
    archive_hidden_folders: bool | None = None
    archive_empty_dirs: bool = True
    archive_format: str = "zip"
    archive_number_pages: bool = True
    archive_encrypt: bool = False
    archive_encrypt_names: bool = True
    audio_mode: str = "encode"
    audio_tracks_mode: str = "all"
    audio_tracks: str = ""
    audio_rate_mode: str = "quality"
    audio_quality: int = 2
    audio_bitrate: int = 192
    audio_sample_rate: int = 0
    audio_channels: str = "preserve"
    audio_action: str = "convert"
    audio_start: str = "0"
    audio_end: str = ""
    audio_ranges: str = ""
    audio_join_order: tuple[str, ...] = ()
    audio_fade_in: float = 0.0
    audio_fade_out: float = 0.0
    audio_gain: float = 0.0
    audio_lufs: float = -16.0
    audio_true_peak: float = -1.5
    audio_tag_mode: str = "keep"
    audio_tag_title: str = ""
    audio_tag_artist: str = ""
    audio_tag_album: str = ""
    audio_tag_track: str = ""
    audio_tag_date: str = ""
    audio_tag_genre: str = ""
    audio_cover_mode: str = "keep"
    audio_cover_path: str = ""
    document_task: str = "merge"
    document_pages: str = ""
    document_split_mode: str = "pages"
    document_ranges: str = ""
    document_order: tuple[str, ...] = ()
    document_rotation: int = 90
    document_dpi: int = 150
    document_quality: int = 85
    document_paper: str = "a4"
    document_margin: float = 10.0
    document_metadata_mode: str = "keep"
    document_title: str = ""
    document_author: str = ""
    document_subject: str = ""
    document_keywords: str = ""
    document_protection: str = "keep"
    document_input_secret: str = ""
    document_output_secret: str = ""
    document_downsample: bool = False
    document_compression: str = "lossless"
    document_image_dpi: int = 150
    document_language: str = "eng"


@dataclass(frozen=True, slots=True)
class PlanItem:
    source: FileRecord
    output: str | None
    status: str
    reason: str
    parent_identity: tuple | None = None
    image_spec: dict | None = None
    video_spec: dict | None = None
    archive_spec: dict | None = None
    audio_spec: dict | None = None
    document_spec: dict | None = None


@dataclass(frozen=True, slots=True)
class OperationPlan:
    request: PlanRequest
    items: tuple[PlanItem, ...]
    warnings: tuple[str, ...] = ()

    @property
    def counts(self):
        return dict(Counter(item.status for item in self.items))


class PlanCancelled(Exception):
    pass


def validate_name(name):
    if not name or name in (".", ".."):
        return "Output name is empty or invalid."
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', name):
        return "Output name contains characters that are not portable to Windows and Linux."
    if name.endswith((".", " ")):
        return "Output name ends with a dot or space."
    stem = name.split(".")[0].upper()
    if re.fullmatch(r"(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", stem):
        return "Output name is reserved on Windows."
    try:
        too_long = len(name.encode("utf-8")) > 255 or len(name.encode("utf-16-le")) // 2 > 255
    except UnicodeError:
        return "Output name contains undecodable filename characters."
    if too_long:
        return "Output name exceeds a filesystem component limit."
    return ""


def _validate_request(request):
    if request.operation not in ("images", "video", "rename", "audio"):
        raise ValueError("Choose Images, Video or Rename.")
    if request.collision not in ("skip", "unique"):
        raise ValueError("Choose Skip or Use a unique name for conflicts.")
    if request.operation == "rename":
        if not isinstance(request.start, int) or not 1 <= request.start <= 999999999:
            raise ValueError("Start number must be between 1 and 999999999.")
        if not isinstance(request.padding, int) or not 1 <= request.padding <= 9:
            raise ValueError("Padding must be between 1 and 9 digits.")
        if request.prefix and re.search(r'[<>:"/\\|?*\x00-\x1f]', request.prefix):
            raise ValueError("The prefix contains an invalid filename character.")
        return None, ()
    formats = {"images": {"jpg", "png", "webp"}, "video": {"mp4", "mkv", "webm"},
               "audio": {"mp3", "m4a", "flac", "wav", "opus"}}
    if request.extension not in formats[request.operation]:
        raise ValueError("Unsupported output format for this operation.")
    if not request.destination.strip():
        if request.operation in ("images", "video", "audio"):
            return None, ()
        raise ValueError("Choose an output folder before previewing.")
    destination = Path(request.destination).expanduser()
    if not destination.is_absolute():
        raise ValueError("Use an absolute output folder path, or choose it with Browse.")
    destination = Path(os.path.abspath(destination))
    warnings = []
    # stat distinguishes missing paths from denied access and broken symlinks.
    parent = destination
    missing = False
    while True:
        try:
            info = parent.stat()
        except NotADirectoryError as error:
            raise ValueError(f"Output path contains a parent that is not a folder: {parent}") from error
        except FileNotFoundError:
            if parent.is_symlink():
                raise ValueError(f"Output path contains a broken link: {parent}")
            missing = True
            if parent == parent.parent:
                raise ValueError("Output folder has no accessible parent.")
            parent = parent.parent
            continue
        except OSError as error:
            raise ValueError(f"Cannot inspect output folder: {error}") from error
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"Output path is not a folder: {parent}")
        break
    # Resolve now to catch symlink loops before the worker plans thousands of rows.
    try:
        path_key(destination)
    except (OSError, RuntimeError) as error:
        raise ValueError(f"Cannot resolve output folder: {error}") from error
    if missing:
        warnings.append("The output folder does not exist yet. Preview does not create it.")
    warnings.append("This previews names only. File decoding and write permissions have not been verified.")
    return destination, tuple(warnings)


def build_plan(records, request: PlanRequest, cancelled=lambda: False):
    if request.operation == "rename":
        from .renaming import plan_rename
        return plan_rename(records, request, cancelled)
    destination, warnings = _validate_request(request)
    reserved = set()
    next_suffix = {}
    # A source is always protected even if it is not compatible with this tool.
    ordered = sorted(records, key=lambda item: item.sort_key)
    seen = set()
    items = []
    sequence = request.start
    for source in ordered:
        if cancelled():
            raise PlanCancelled()
        if source.sample:
            source_key = source.path
        else:
            try:
                source_key = path_key(source.path)
            except (OSError, RuntimeError) as error:
                items.append(PlanItem(source, None, "Invalid", f"Cannot resolve source: {error}"))
                continue
        if source_key in seen:
            items.append(PlanItem(source, None, "Skipped", "Duplicate input path."))
            continue
        seen.add(source_key)
        expected_kind = {"images": "Image", "video": "Video"}.get(request.operation)
        if request.operation == "audio" and source.kind not in ("Audio", "Video"):
            items.append(PlanItem(source, None, "Incompatible", "Choose audio or video files for Audio."))
            continue
        if expected_kind and source.kind != expected_kind:
            items.append(PlanItem(source, None, "Incompatible",
                                  f"This file is {source.kind.lower()}; the selected tool requires {expected_kind.lower()} files."))
            continue
        if not source.sample:
            path = Path(source.path)
            try:
                if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                    raise ValueError("Source became a link. Scan it again.")
                current = path.stat()
                if not stat.S_ISREG(current.st_mode):
                    raise ValueError("Source is no longer a regular file.")
                if source.modified_ns is not None and (
                    current.st_size != source.size or current.st_mtime_ns != source.modified_ns
                    or (source.device is not None and current.st_dev != source.device)
                    or (source.inode is not None and current.st_ino != source.inode)
                ):
                    raise ValueError("Source changed since discovery. Remove it from the list and add it again.")
            except (OSError, ValueError) as error:
                items.append(PlanItem(source, None, "Invalid", str(error)))
                continue
        if request.operation == "rename":
            name = f"{request.prefix}{sequence:0{request.padding}d}{Path(source.name).suffix}"
            sequence += 1
            output = Path(source.path).parent / name if not source.sample else Path(name)
        else:
            name = f"{Path(source.name).stem}.{request.extension}"
            output = (destination if destination is not None else Path(source.path).parent) / name
        invalid = validate_name(name)
        if invalid:
            items.append(PlanItem(source, None, "Invalid", invalid))
            continue
        if source.sample:
            # Generated rows are never real filesystem inputs.
            items.append(PlanItem(source, str(output), "Sample",
                                  "Illustrative sample only; no source or output file exists."))
            continue
        if request.operation == "rename" and str(output) == source.path:
            items.append(PlanItem(source, str(output), "Unchanged", "Already has this name."))
            reserved.add(source_key)
            continue
        output_key = path_key(output)
        conflict = output_key in reserved or os.path.lexists(output)
        reason = "Name preview only; processing is not available in this release."
        if conflict:
            if request.collision == "skip":
                items.append(PlanItem(source, str(output), "Skipped",
                                      "Output already exists or is reserved by an earlier item."))
                continue
            base = output
            base_key = output_key
            counter = next_suffix.get(base_key, 1)
            attempts = 0
            while output_key in reserved or os.path.lexists(output):
                if cancelled():
                    raise PlanCancelled()
                output = base.with_name(f"{base.stem}_{counter}{base.suffix}")
                invalid = validate_name(output.name)
                if invalid:
                    break
                output_key = path_key(output)
                counter += 1
                attempts += 1
                if attempts > 100000:
                    invalid = "Too many filename conflicts; choose a different destination or prefix."
                    break
            if invalid:
                items.append(PlanItem(source, None, "Invalid", invalid))
                continue
            next_suffix[base_key] = counter
            reason = "A unique name avoids an existing output or another item in this batch."
        reserved.add(output_key)
        items.append(PlanItem(source, str(output), "Planned", reason))
    return OperationPlan(request, tuple(items), warnings)
