"""Conservative stream-copy policy and bounded packet-by-packet verification."""
import math
import queue
import threading

from .jobs import checkpoint
from .processes import run_process
from .video_formats import CONTAINERS
from .video_tracks import labels, label_args, verify_labels

VIDEO = {"mp4": {"h264", "hevc"}, "mkv": {"h264", "hevc", "vp9"}, "webm": {"vp9"}}
AUDIO = {"mp4": {"aac", "mp3"}, "mkv": {"aac", "mp3", "opus", "vorbis", "flac", "ac3", "eac3"}, "webm": {"opus", "vorbis"}}


def remux_tracks(video, audios, subtitles, request):
    container = request.extension
    if video["codec_name"] not in VIDEO[container]:
        raise ValueError(f"{container.upper()} remux cannot keep {video['codec_name']} video. Choose a compatible container or Convert video.")
    if video["rotation"] % 360 and container != "mp4":
        raise ValueError("This remux phase preserves rotation only in MP4. Choose MP4 or Convert video to normalize the picture.")
    sounds = []
    preferred = next((n for n, stream in enumerate(audios) if stream.get("default")), 0)
    for n, stream in enumerate(audios):
        if stream["codec_name"] not in AUDIO[container]:
            raise ValueError(f"{container.upper()} remux cannot keep {stream['codec_name']} audio #{stream['index']}. Choose MKV, omit this track, or Convert video.")
        track = labels(stream, container)
        track.update(codec=stream["codec_name"], default=int(n == preferred))
        sounds.append(track)
    return sounds, [dict(**labels(stream, container), codec=stream["codec_name"]) for stream in subtitles]


def remux_command(spec, temp):
    container = spec["container"]
    args = [spec["tools"]["ffmpeg"], "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-nostats", "-y",
            "-progress", "pipe:1", "-stats_period", "0.2", "-threads", str(spec["threads"]),
            "-protocol_whitelist", "file,pipe", "-err_detect", "explode", "-i", spec["source"]]
    external = spec.get("external_subtitle")
    if external:
        args += ["-protocol_whitelist", "file,pipe", "-itsoffset", str(spec["external_offset"]),
                 "-f", external["input_format"], "-sub_charenc", "UTF-8", "-i", external["source"]]
    args += ["-map", f"0:{spec['video_index']}", "-c:v", "copy"]
    for kind, tracks in (("a", spec["audio_tracks"]), ("s", spec["subtitle_tracks"])):
        for ordinal, track in enumerate(tracks):
            args += ["-map", f"{track.get('input', 0)}:{track['index']}", f"-c:{kind}:{ordinal}", track["codec"] if track.get("input") else "copy"]
        if not tracks:
            args += ["-an" if kind == "a" else "-sn"]
    args += ["-dn", "-map_metadata", "-1", "-map_metadata:s", "-1", "-map_chapters", "-1"]
    for kind, tracks in (("a", spec["audio_tracks"]), ("s", spec["subtitle_tracks"])):
        for ordinal, track in enumerate(tracks):
            args += label_args(track, kind, ordinal, container)
    if container == "mp4":
        args += ["-movflags", "+faststart"]
    else:
        args += ["-default_mode", "passthrough"]
    return args + ["-f", CONTAINERS[container], str(temp)]


def verify_remux_output(spec, info):
    if CONTAINERS[spec["container"]] not in info["format"].split(","):
        raise ValueError("Remux output container failed verification.")
    expected = [("video", {"index": spec["video_index"], "codec": spec["codec"]})]
    expected += [("audio", track) for track in spec["audio_tracks"]]
    expected += [("subtitle", track) for track in spec["subtitle_tracks"]]
    if len(info["streams"]) != len(expected):
        raise ValueError("Remux stream count does not match the saved selection.")
    for output, (kind, track) in zip(info["streams"], expected):
        if output["codec_type"] != kind or output["codec_name"] != track["codec"]:
            raise ValueError("Remux stream order or codec changed.")
        if kind != "video":
            verify_labels(track, output)
        if track.get("input"):
            continue
        source = next(s for s in spec["video"]["streams"] if s["index"] == track["index"])
        keys = ("width", "height", "pix_fmt", "sample_aspect_ratio", "rotation") if kind == "video" else ("channels", "sample_rate", "channel_layout") if kind == "audio" else ()
        if any(source.get(key) != output.get(key) for key in keys):
            raise ValueError(f"Remux {kind} properties changed.")


def packet(line):
    values = dict(part.split("=", 1) for part in line.split("|") if "=" in part)
    if "data_hash" not in values:
        raise ValueError("Cannot verify copied packet payloads.")
    result = {"hash": values["data_hash"]}
    for key in ("pts_time", "dts_time", "duration_time"):
        try:
            value = float(values.get(key, "N/A"))
            result[key] = value if math.isfinite(value) else None
        except ValueError:
            result[key] = None
    return result


def verify_packet_pair(source, target, source_start, output_start, kind, ordinal, last):
    if source["hash"] != target["hash"]:
        raise ValueError("Copied packet payload changed; remux output was not committed.")
    for key in ("pts_time", "dts_time"):
        before, after = source[key], target[key]
        if before is None or after is None:
            if key == "dts_time" and kind == "video" and ordinal <= 16:
                continue  # Some demuxers cannot infer the first reordered DTS values.
            raise ValueError("Cannot verify copied packet timing.")
        if abs((before - source_start) - (after - output_start)) > .002:
            raise ValueError("Copied packet timing changed beyond 2 ms.")
    before, after = source["duration_time"], target["duration_time"]
    if before is not None and after is not None:
        tolerance = max(.002, before, after) if kind == "audio" and (ordinal == 1 or last) else .002
        if abs(before - after) > tolerance:
            raise ValueError("Copied packet duration changed.")


def verify_packet_stream(spec, path, source, output, source_start, output_start, cancelled):
    """Two supervised probes, bounded queue; no whole-movie packet lists in RAM."""
    messages, stop = queue.Queue(maxsize=32), threading.Event()
    errors = []

    def stopped():
        return stop.is_set() or cancelled()

    def send(value):
        while True:
            checkpoint(stopped)
            try:
                messages.put(value, timeout=.1)
                return
            except queue.Full:
                pass

    def command(filename, index):
        return [spec["tools"]["ffprobe"], "-v", "error", "-protocol_whitelist", "file,pipe", "-select_streams", str(index),
                "-show_packets", "-show_data_hash", "sha256", "-show_entries", "packet=pts_time,dts_time,duration_time,data_hash:packet_side_data=",
                "-of", "compact=p=0:nk=0", str(filename)]

    def produce():
        try:
            run_process(command(spec["source"], source["index"]), stopped, on_line=lambda line: send(packet(line)) if line else None)
            send(None)
        except Exception as error:
            errors.append(error)

    worker = threading.Thread(target=produce, name="remux-packet-source", daemon=True)
    worker.start()

    def receive():
        while True:
            checkpoint(cancelled)
            if errors:
                raise errors[0]
            try:
                return messages.get(timeout=.1)
            except queue.Empty:
                if not worker.is_alive():
                    raise ValueError("Source packet inspection ended unexpectedly.")

    count, pending, previous_dts = 0, None, None

    def compare(line):
        nonlocal count, pending, previous_dts
        if not line:
            return
        before, after = receive(), packet(line)
        if before is None:
            raise ValueError("Copied stream has extra packets.")
        if pending:
            verify_packet_pair(*pending, source_start, output_start, source["codec_type"], count, False)
        dts = after["dts_time"]
        if dts is not None:
            if previous_dts is not None and dts <= previous_dts:
                raise ValueError("Copied stream decoding timestamps are not increasing.")
            previous_dts = dts
        count += 1
        pending = (before, after)

    try:
        run_process(command(path, output["index"]), cancelled, on_line=compare)
        if receive() is not None:
            raise ValueError("Copied stream lost packets.")
        if not pending:
            raise ValueError("Selected copied stream has no packets.")
        verify_packet_pair(*pending, source_start, output_start, source["codec_type"], count, True)
    finally:
        stop.set()
        worker.join(timeout=10)
        if worker.is_alive():
            raise ValueError("Packet inspection did not stop cleanly.")
    return count


def verify_remux_packets(spec, path, info, cancelled):
    source_video = next(s for s in spec["video"]["streams"] if s["index"] == spec["video_index"])
    output_video = info["streams"][0]
    if source_video["start"] is None or output_video["start"] is None:
        raise ValueError("Remux packet verification needs known video start times.")
    indices = [spec["video_index"], *[track["index"] for track in spec["audio_tracks"]]]
    for index, output in zip(indices, info["streams"]):
        source = next(s for s in spec["video"]["streams"] if s["index"] == index)
        verify_packet_stream(spec, path, source, output, source_video["start"], output_video["start"], cancelled)
