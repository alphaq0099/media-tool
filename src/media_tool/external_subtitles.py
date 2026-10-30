"""Inspected UTF-8 sidecars, explicit subtitle encoding and text/timing verification."""
import html
import math
from pathlib import Path
import re

from .jobs import check_source, hash_file, identity, regular
from .processes import run_process

FORMATS = {".srt": "srt", ".vtt": "webvtt"}
OUTPUT_CODECS = {"mp4": "mov_text", "mkv": "subrip", "webm": "webvtt"}
STAMP = r"(\d+):(\d{2}):(\d{2}),(\d{3})"
TIMING = re.compile(r"^" + STAMP + r" --> " + STAMP + r"(?:\s.*)?$")


def subtitle_text(path, index, tools, cancelled, input_format=None):
    args = [tools["ffmpeg"], "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-copyts", "-protocol_whitelist", "file,pipe"]
    if input_format:
        args += ["-f", input_format, "-sub_charenc", "UTF-8"]
    data = run_process(args + ["-i", str(path), "-map", f"0:{index}", "-c:s", "srt", "-f", "srt", "pipe:1"], cancelled, timeout=60)
    cues = []
    for block in re.split(r"\n\s*\n", data.replace("\r\n", "\n").strip()):
        lines = block.splitlines()
        if not lines:
            continue
        if len(lines) < 3 or not lines[0].isdigit() or not (match := TIMING.fullmatch(lines[1])):
            raise ValueError("Cannot read subtitle cue text and timing.")
        values = list(map(int, match.groups()))
        times = [values[n] * 3600 + values[n+1] * 60 + values[n+2] + values[n+3] / 1000 for n in (0, 4)]
        if not all(math.isfinite(value) for value in times) or times[1] <= times[0]:
            raise ValueError("Subtitle cues need finite, increasing start/end times.")
        text = "\n".join(lines[2:])
        # Formatting can change when adapting SRT/WebVTT to timed text. Verify
        # visible text separately; never promise to preserve font/style markup.
        text = re.sub(r"</?(?:b|i|u|font)\b[^>]*>", "", text, flags=re.I)
        cues.append((times[0], times[1], html.unescape(text)))
    return cues


def prepare_external(request, tools, probe, cancelled):
    if not request.video_external_enabled:
        return None
    if not request.video_external_subtitle.strip():
        raise ValueError("Choose an external subtitle file, or turn off Add subtitle file.")
    path = Path(request.video_external_subtitle.strip()).resolve(strict=True)
    if path.suffix.lower() not in FORMATS:
        raise ValueError("External subtitles currently support UTF-8 .srt and .vtt files.")
    stat = regular(path)
    if not 0 < stat.st_size <= 4 * 1024 * 1024:
        raise ValueError("External subtitle files must be nonempty and no larger than 4 MiB.")
    external = dict(source=str(path), identity=identity(stat), digest=hash_file(path, cancelled),
                    input_format=FORMATS[path.suffix.lower()])
    try:
        with path.open("rb") as handle:
            check_source(external, handle)
            content = handle.read(4 * 1024 * 1024 + 1).decode("utf-8-sig")
            check_source(external, handle)
        if "\0" in content:
            raise UnicodeError()
    except UnicodeError as error:
        raise ValueError("Save the external subtitle file as UTF-8, then try again.") from error
    info = probe(path, tools, cancelled, input_format=external["input_format"])
    if len(info["streams"]) != 1 or info["streams"][0]["codec_type"] != "subtitle" or info["streams"][0]["codec_name"] not in ("subrip", "webvtt"):
        raise ValueError("External file must contain one SRT or WebVTT subtitle stream.")
    index = info["streams"][0]["index"]
    cues = subtitle_text(path, index, tools, cancelled, external["input_format"])
    if not cues:
        raise ValueError("External subtitle file has no readable cues.")
    if any(start < 0 for start, _, _ in cues):
        raise ValueError("External subtitle cue times must be zero or later.")
    if request.extension == "mp4" and any(first[1] > second[0] for first, second in zip(cues, cues[1:])):
        raise ValueError("MP4 needs non-overlapping external subtitle cues. Correct their timing, or choose MKV/WebM.")
    external.update(index=index, cue_count=len(cues), last_end=max(end for _, end, _ in cues),
                    title=path.stem, language="und", default=0, forced=0, input=1,
                    codec=OUTPUT_CODECS[request.extension])
    check_external(external, cancelled)
    return external


def check_external(external, cancelled):
    if external is None:
        return
    try:
        check_source(external)
        if hash_file(external["source"], cancelled) != external["digest"]:
            raise ValueError("Subtitle contents changed.")
    except (ValueError, OSError) as error:
        raise ValueError("External subtitle changed or is unavailable. Preview again; no new output was committed.") from error


def verify_external(spec, path, info, cancelled):
    external = spec.get("external_subtitle")
    if external is None:
        return
    check_external(external, cancelled)
    subtitles = [s for s in info["streams"] if s["codec_type"] == "subtitle"]
    output = subtitles[-1]
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    before = subtitle_text(external["source"], external["index"], spec["tools"], cancelled, external["input_format"])
    after = subtitle_text(path, output["index"], spec["tools"], cancelled)
    if len(before) != len(after):
        raise ValueError("Embedded subtitle cue count changed.")
    if video["start"] is None:
        raise ValueError("Cannot verify embedded subtitle timing without a video start time.")
    for source, target in zip(before, after):
        if source[2] != target[2]:
            raise ValueError("Embedded subtitle text changed.")
        if any(abs(source[n] - (target[n] - video["start"])) > .01 for n in (0, 1)):
            raise ValueError("Embedded subtitle timing changed beyond 10 ms.")
