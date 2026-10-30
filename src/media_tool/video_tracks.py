"""Explicit batch track selection, container policy and copied-subtitle verification."""
import json
import re

from .processes import run_process
from .video_formats import opus_settings

SUBTITLES = {"mp4": {"mov_text"}, "mkv": {"subrip", "ass", "ssa", "webvtt"}, "webm": {"webvtt"}}


def track_numbers(text):
    values = text.split(",")
    if not values or any(not re.fullmatch(r"\s*[0-9]{1,2}\s*", value) for value in values):
        raise ValueError("Enter track numbers separated by commas, such as 1, 3.")
    result = [int(value) for value in values]
    if any(value < 1 for value in result) or len(set(result)) != len(result):
        raise ValueError("Track numbers must be 1–99 without duplicates.")
    return result


def choose(streams, mode, text, kind, single=1):
    if mode == "none" or (mode == "single" and single == 0):
        return []
    if mode == "all":
        return list(streams)
    if mode == "single":
        if single == 1 and not streams:
            return []
        numbers = [single]
    elif mode == "custom":
        numbers = track_numbers(text)
    else:
        raise ValueError(f"Choose a supported {kind.lower()} selection.")
    if any(value < 1 or value > len(streams) for value in numbers):
        missing = next(value for value in numbers if value < 1 or value > len(streams))
        raise ValueError(f"{kind} track {missing} is unavailable; this file has {len(streams)} {kind.lower()} tracks.")
    return [streams[value - 1] for value in numbers]


def selected_tracks(info, request):
    audios = choose([s for s in info["streams"] if s["codec_type"] == "audio"],
        request.video_audio_mode, request.video_audio_tracks, "Audio", request.video_audio)
    subtitles = choose([s for s in info["streams"] if s["codec_type"] == "subtitle"],
        request.video_subtitle_mode, request.video_subtitle_tracks, "Subtitle")
    for audio in audios:
        if not audio["channels"] or not 1 <= audio["channels"] <= 6:
            raise ValueError("Selected audio must have between one and six channels.")
    for subtitle in subtitles:
        if subtitle["codec_name"] not in SUBTITLES[request.extension]:
            names = ", ".join(sorted(SUBTITLES[request.extension]))
            raise ValueError(f"{request.extension.upper()} cannot keep selected {subtitle['codec_name']} subtitles in this phase. "
                             f"Supported copied subtitles: {names}. Choose a compatible container or deselect this track.")
    return audios, subtitles


def labels(stream, container):
    title = stream.get("title", "")
    language = stream.get("language") or "und"
    if len(title) > 512 or len(language) > 63 or "\0" in title + language:
        raise ValueError("Selected track labels exceed the supported length or contain a null character.")
    if container == "mp4" and not re.fullmatch("[a-z]{3}", language):
        raise ValueError(f"MP4 needs a three-letter track language code; cannot preserve {language!r}. Choose MKV.")
    return dict(index=stream["index"], language=language, title=title,
                default=int(stream.get("default", 0)), forced=int(stream.get("forced", 0)))


def track_specs(audios, subtitles, request):
    audio_specs = []
    default = next((index for index, audio in enumerate(audios) if audio.get("default")), 0)
    for index, audio in enumerate(audios):
        output = labels(audio, request.extension)
        output.update(opus_settings(audio) if request.extension == "webm" else
            dict(codec="aac", bitrate=(request.video_audio_kbps if request.video_mode == "target_size" else 192) * 1000))
        output["default"] = int(index == default)
        audio_specs.append(output)
    subtitle_specs = [dict(**labels(stream, request.extension), codec=stream["codec_name"]) for stream in subtitles]
    return audio_specs, subtitle_specs


def audio_specs(spec):
    if "audio_tracks" in spec:
        return spec["audio_tracks"]
    if spec["audio_index"] is None:
        return []
    track = dict(index=spec["audio_index"], codec="aac", bitrate=spec.get("budget", {}).get("audio_bitrate", 192_000))
    track.update(spec.get("audio_output", {}))
    return [track]


def label_args(track, kind, ordinal, container):
    flags = "+".join(name for name in ("default", "forced") if track[name]) or "0"
    args = [f"-metadata:s:{kind}:{ordinal}", f"language={track['language']}",
            f"-metadata:s:{kind}:{ordinal}", f"title={track['title']}", f"-disposition:{kind}:{ordinal}", flags]
    if container == "mp4" and track["title"]:
        args += [f"-metadata:s:{kind}:{ordinal}", f"handler_name={track['title']}"]
    return args


def verify_labels(track, output):
    if (output.get("language") or "und") != track["language"] or (track["title"] and output.get("title") != track["title"]):
        raise ValueError("Selected track language or title failed verification.")
    if any(int(output.get(flag, 0)) != track[flag] for flag in ("default", "forced")):
        raise ValueError("Selected track default/forced flags failed verification.")


def subtitle_packets(path, index, tools, cancelled):
    data = run_process([tools["ffprobe"], "-v", "error", "-protocol_whitelist", "file,pipe", "-select_streams", str(index),
        "-show_packets", "-show_data_hash", "sha256", "-show_entries", "packet=pts_time,duration_time,data_hash,size",
        "-of", "json", str(path)], cancelled, timeout=60)
    return json.loads(data).get("packets", [])


def verify_subtitles(spec, path, info, cancelled):
    expected = spec.get("subtitle_tracks", [])
    actual = [s for s in info["streams"] if s["codec_type"] == "subtitle"]
    source_video = next(s for s in spec["video"]["streams"] if s["index"] == spec["video_index"])
    output_video = next(s for s in info["streams"] if s["codec_type"] == "video")
    for track, output in zip(expected, actual):
        if track.get("input"):
            continue
        before = subtitle_packets(spec["source"], track["index"], spec["tools"], cancelled)
        after = subtitle_packets(path, output["index"], spec["tools"], cancelled)
        if track["codec"] == "mov_text":
            before = [p for p in before if int(p.get("size", 0)) > 2]
            after = [p for p in after if int(p.get("size", 0)) > 2]
        if len(before) != len(after):
            raise ValueError("Copied subtitle cue count changed.")
        for source, target in zip(before, after):
            if not source.get("data_hash") or source["data_hash"] != target.get("data_hash"):
                raise ValueError("Copied subtitle content changed.")
            try:
                shift = (float(source["pts_time"]) - (source_video["start"] or 0)) - (float(target["pts_time"]) - (output_video["start"] or 0))
                duration = float(source["duration_time"]) - float(target["duration_time"])
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError("Cannot verify copied subtitle timing.") from error
            if abs(shift) > .01 or abs(duration) > .01:
                raise ValueError("Copied subtitle timing changed beyond 10 ms.")
