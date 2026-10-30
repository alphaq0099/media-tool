"""Supported Unicode tags and artwork on separate outputs; encoded audio is copied."""
from dataclasses import replace
from datetime import date
import base64
import hashlib
from io import BytesIO
import os
from pathlib import Path
import re

from .audio_edit import reserve_output
from .domain import path_key
from .jobs import check_source, checkpoint, hash_file, identity, regular
from .planning import OperationPlan, PlanCancelled

FIELDS = ("title", "artist", "album", "track", "date", "genre")
ID3_KEYS = dict(zip(FIELDS, ("TIT2", "TPE1", "TALB", "TRCK", "TDRC", "TCON")))
MP4_KEYS = dict(zip(FIELDS, ("\xa9nam", "\xa9ART", "\xa9alb", "trkn", "\xa9day", "\xa9gen")))
VORBIS_KEYS = dict(zip(FIELDS, ("title", "artist", "album", "tracknumber", "date", "genre")))
MAX_COVER = 5 * 1024**2


def cover_info(data):
    from PIL import Image
    if len(data) > MAX_COVER:
        raise ValueError("Cover art exceeds the 5 MiB limit.")
    with Image.open(BytesIO(data)) as image:
        if image.format not in ("JPEG", "PNG") or image.width*image.height > 20_000_000:
            raise ValueError("Cover art must be JPEG/PNG and no larger than 20 megapixels.")
        result = dict(mime="image/jpeg" if image.format == "JPEG" else "image/png", width=image.width,
                      height=image.height, depth=24, sha256=hashlib.sha256(data).hexdigest())
        image.verify()
    return result


def read_tags(path):
    import mutagen
    from mutagen.id3 import ID3
    from mutagen.mp4 import MP4
    from mutagen.flac import FLAC, Picture
    audio = mutagen.File(str(path))
    if audio is None:
        raise ValueError("Tags require a standalone MP3, M4A, FLAC, WAV or Opus file. Extract video soundtracks first.")
    values, pictures = {key: "" for key in FIELDS}, []
    tags = audio.tags or {}
    if isinstance(tags, ID3):
        for key, tag in ID3_KEYS.items():
            frames = tags.getall(tag)
            values[key] = str(frames[0].text[0]) if frames and frames[0].text else ""
        pictures = [bytes(frame.data) for frame in tags.getall("APIC")]
    elif isinstance(audio, MP4):
        for key, tag in MP4_KEYS.items():
            if tag in tags and tags[tag]:
                value = tags[tag][0]
                values[key] = (f"{value[0]}/{value[1]}" if value[1] else str(value[0])) if key == "track" else str(value)
        pictures = [bytes(data) for data in tags.get("covr", [])]
    else:
        for key, tag in VORBIS_KEYS.items():
            values[key] = str(tags.get(tag, [""])[0])
        if isinstance(audio, FLAC):
            pictures = [p.data for p in audio.pictures]
        else:
            pictures = [Picture(base64.b64decode(value)).data for value in tags.get("metadata_block_picture", [])]
    return values, pictures


def plan_tags(records, request, cancelled=lambda: False):
    from .audio import plan_conversion
    if request.audio_tag_mode not in ("keep", "edit", "remove") or request.audio_cover_mode not in ("keep", "replace", "remove"):
        raise ValueError("Choose supported tag and artwork policies.")
    supplied = {key: getattr(request, "audio_tag_"+key).strip() if request.audio_tag_mode=='edit' else '' for key in FIELDS}
    if any(len(value) > 512 or "\0" in value for value in supplied.values()):
        raise ValueError("Tag values must be at most 512 characters and contain no nulls.")
    if supplied["track"] and not re.fullmatch(r"[1-9]\d{0,4}(?:/[1-9]\d{0,4})?", supplied["track"]):
        raise ValueError("Track must be a positive number or number/total, for example 3/12.")
    if supplied['track'] and any(int(v)>65535 for v in supplied['track'].split('/')):
        raise ValueError('Track numbers must be at most 65535 for portable tags.')
    if supplied['date']:
        try:
            if not re.fullmatch(r'\d{4}(?:-\d{2}(?:-\d{2})?)?',supplied['date']):raise ValueError()
            parts=[int(v) for v in supplied['date'].split('-')]
            date(*(parts+[1]*(3-len(parts))))
        except ValueError:
            raise ValueError('Use a valid year or date: YYYY, YYYY-MM or YYYY-MM-DD.') from None
    external = None
    if request.audio_cover_mode == "replace":
        path = str(Path(request.audio_cover_path).expanduser().resolve(strict=True))
        info = regular(path)
        if info.st_size > MAX_COVER:
            raise ValueError("Cover art exceeds the 5 MiB limit.")
        external = dict(source=path, identity=identity(info), digest=hash_file(path, cancelled), info=cover_info(Path(path).read_bytes()))
    base = plan_conversion(records, replace(request, audio_action="convert", audio_mode="copy", audio_sample_rate=0,
        audio_channels="preserve", audio_fade_in=0, audio_fade_out=0, collision="unique"), cancelled)
    reserved = {path_key(r.path) for r in records if not r.sample}
    items = []
    for item in base.items:
        if item.status != "Planned":
            items.append(item)
            continue
        try:
            values, pictures = read_tags(item.source.path)
            if request.audio_tag_mode == "remove":
                values = {key: "" for key in FIELDS}
            elif request.audio_tag_mode == "edit":
                values.update({key:value for key,value in supplied.items() if value})
            if request.audio_cover_mode == "keep" and len(pictures) > 1:
                raise ValueError("This file has multiple covers. Choose Replace or Remove to resolve them.")
            expected = external["info"] if external else cover_info(pictures[0]) if pictures and request.audio_cover_mode == "keep" else None
            spec = dict(item.audio_spec, tags=dict(values=values, cover_mode=request.audio_cover_mode, cover=expected, external=external))
            output, status = reserve_output(Path(spec["parent"]), f"{Path(item.source.name).stem}_tags.{request.extension}", request.collision, reserved, cancelled)
            spec["output"] = output
            reason = "; ".join(f"{key}: {value or '(empty)'}" for key,value in values.items()) + f"; cover: {request.audio_cover_mode}; encoded audio copied"
            items.append(replace(item, output=output, status=status, reason=reason, audio_spec=spec))
        except Exception as error:
            if cancelled():
                raise PlanCancelled() from error
            items.append(replace(item, status="Invalid", reason=str(error), audio_spec=None))
    return OperationPlan(request, tuple(items), ("Keeps only the six supported tags and selected cover. Other tags/chapters are omitted.",))


def apply_tags(spec, path, cancelled, owner):
    import mutagen
    from mutagen.id3 import ID3, APIC
    import mutagen.id3 as id3
    from mutagen.mp4 import MP4, MP4Cover
    from mutagen.flac import FLAC, Picture
    checkpoint(cancelled)
    expected = spec["tags"]
    picture = None
    if expected["cover"]:
        if expected["external"]:
            external = expected["external"]
            check_source(external)
            if hash_file(external["source"], cancelled) != external["digest"]:
                raise ValueError("Cover art changed after preview.")
            picture = Path(external["source"]).read_bytes()
        else:
            _, pictures = read_tags(spec["source"])
            picture = pictures[0]
        if cover_info(picture) != expected["cover"]:
            raise ValueError("Cover art differs from the preview.")
    with Path(path).open("rb+") as handle:
        if tuple(str(v) for v in identity(os.fstat(handle.fileno()))[:2])!=tuple(owner):
            raise ValueError('Temporary audio was replaced before tag writing; file was kept.')
        audio = mutagen.File(handle)
        if audio.tags is None:
            audio.add_tags()
        audio.tags.clear()
        values = expected["values"]
        if isinstance(audio.tags, ID3):
            for key, frame in ID3_KEYS.items():
                if values[key]:
                    audio.tags.add(getattr(id3, frame)(encoding=3, text=[values[key]]))
            if picture:
                audio.tags.add(APIC(encoding=3, mime=expected["cover"]["mime"], type=3, desc="Cover", data=picture))
        elif isinstance(audio, MP4):
            for key, tag in MP4_KEYS.items():
                if values[key]:
                    parts = values[key].split("/") if key == "track" else None
                    audio.tags[tag] = [(int(parts[0]),int(parts[1]) if len(parts)>1 else 0)] if parts else [values[key]]
            if picture:
                audio.tags["covr"] = [MP4Cover(picture, imageformat=MP4Cover.FORMAT_JPEG if expected["cover"]["mime"] == "image/jpeg" else MP4Cover.FORMAT_PNG)]
        else:
            for key, tag in VORBIS_KEYS.items():
                if values[key]:
                    audio.tags[tag] = [values[key]]
            block = Picture()
            if isinstance(audio, FLAC):
                audio.clear_pictures()
            if picture:
                block.type, block.mime, block.desc = 3, expected["cover"]["mime"], "Cover"
                block.width, block.height, block.depth = expected["cover"]["width"], expected["cover"]["height"], 24
                block.data = picture
                if isinstance(audio, FLAC):
                    audio.add_picture(block)
                else:
                    audio.tags["metadata_block_picture"] = [base64.b64encode(block.write()).decode("ascii")]
        handle.seek(0)
        audio.save(handle)
    checkpoint(cancelled)


def verify_tags(spec, path, cancelled):
    checkpoint(cancelled)
    values, pictures = read_tags(path)
    if values != spec["tags"]["values"]:
        raise ValueError("Written tags do not match the preview.")
    actual = cover_info(pictures[0]) if len(pictures) == 1 else None
    if actual != spec["tags"]["cover"] or len(pictures) > 1:
        raise ValueError("Written artwork does not match the preview.")
