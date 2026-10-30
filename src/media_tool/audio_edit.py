"""Audio edit planning and bounded packet verification; originals are never modified."""
from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
import queue
import subprocess
import threading

from .domain import path_key
from .jobs import check_source, checkpoint
from .planning import OperationPlan, PlanCancelled, validate_name
from .processes import run_process
from .video import number

MAX_JOIN = 32
MAX_RANGES = 200


def source_specs(spec):
    return spec.get("sources", [spec])


def seconds(text, allow_end=False):
    text = str(text).strip()
    if allow_end and text.lower() in ("", "end"):
        return None
    if not re.fullmatch(r"\d+(?::\d{1,2}){0,2}(?:\.\d{1,6})?", text):
        raise ValueError("Enter seconds, MM:SS or HH:MM:SS, for example 90 or 01:30.5.")
    parts = [float(p) for p in text.split(":")]
    if len(parts) > 1 and any(p >= 60 for p in parts[1:]):
        raise ValueError("Minutes/seconds after a colon must be below 60.")
    result = sum(p * 60**i for i, p in enumerate(reversed(parts)))
    if not math.isfinite(result) or result > 7 * 86400:
        raise ValueError("Audio times must be within seven days.")
    return result


def parse_ranges(request):
    if request.audio_action == "trim":
        return [(seconds(request.audio_start), seconds(request.audio_end, True))]
    if request.audio_action != "split":
        return [(0., None)]
    lines = [line.strip() for line in re.split(r"[\n;]+", request.audio_ranges) if line.strip()]
    if not lines or len(lines) > MAX_RANGES:
        raise ValueError(f"Enter 1–{MAX_RANGES} split ranges, one start - end range per line.")
    ranges = []
    for line in lines:
        pair = line.split("-")
        if len(pair) != 2:
            raise ValueError("Each split line needs start - end, for example 0 - 00:30.")
        ranges.append((seconds(pair[0]), seconds(pair[1], True)))
    for index, (start, end) in enumerate(ranges):
        if end is not None and end <= start:
            raise ValueError("Each range must end after its start.")
        if index and (ranges[index-1][1] is None or start < ranges[index-1][1]):
            raise ValueError("Split ranges must be in time order without overlaps.")
    return ranges


def fades(request, duration):
    values = (request.audio_fade_in, request.audio_fade_out)
    if any(not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 3600 for v in values):
        raise ValueError("Fade durations must be between 0 and 3600 seconds.")
    if sum(values) > duration + 1e-8:
        raise ValueError("Fade in plus fade out must not exceed the resulting duration.")
    if request.audio_mode == "copy" and any(values):
        raise ValueError("Fades require Convert audio. Encoded audio cannot be faded without re-encoding.")
    return dict(fade_in=values[0], fade_out=values[1])


def packet_scan(path, stream, tools, cancelled, start=None, end=None, digest=None, on_packet=None):
    """A rolling payload digest and timing bounds, never a whole-song packet list."""
    digest = digest if digest is not None else hashlib.sha256()
    origin = stream.get("start")
    count, first, last, previous, largest = 0, None, None, None, 0
    def collect(line):
        nonlocal count, first, last, origin, previous, largest
        values = dict(v.split("=", 1) for v in line.split("|") if "=" in v)
        pts, dts, duration = (number(values.get(k)) for k in ("pts_time", "dts_time", "duration_time"))
        if pts is None or dts is None or duration is None or duration <= 0:
            raise ValueError("Copy editing needs known packet timestamps and durations. Choose Convert audio.")
        if origin is None:
            origin = pts
        relative = pts - origin
        if start is not None and relative < start - 1e-7 or end is not None and relative >= end - 1e-7:
            return
        if previous is not None and dts <= previous:
            raise ValueError("Copy editing needs increasing packet timestamps. Choose Convert audio.")
        previous = dts
        payload = values.get("data_hash", "")
        if not re.fullmatch(r"SHA256:[a-fA-F0-9]{64}", payload):
            raise ValueError("Could not inspect encoded packet payloads.")
        digest.update(bytes.fromhex(payload.split(":", 1)[1]))
        if on_packet is not None:
            on_packet(dict(pts=relative, dts=dts-origin, duration=duration, hash=payload))
        count += 1
        first = relative if first is None else min(first, relative)
        last = max(last if last is not None else relative, relative + duration)
        largest = max(largest, duration)
    run_process([tools["ffprobe"], "-v", "error", "-protocol_whitelist", "file,pipe", "-select_streams", str(stream["index"]),
                 "-show_packets", "-show_data_hash", "sha256", "-show_entries",
                 "packet=pts_time,dts_time,duration_time,data_hash:packet_side_data=", "-of", "compact=p=0:nk=0", str(path)],
                cancelled, on_line=lambda line: collect(line) if line else None)
    if not count:
        raise ValueError("The selected range contains no complete audio packets. Choose Convert or a longer range.")
    return dict(count=count, first=first, end=last, duration=last-first, packet_limit=largest, digest=digest.hexdigest(), origin=origin)


def copy_signature(spec, cancelled):
    text = run_process([spec["tools"]["ffprobe"], "-v", "error", "-protocol_whitelist", "file,pipe", "-show_streams",
        "-show_data_hash", "sha256", "-show_entries",
        "stream=codec_type,codec_name,codec_tag_string,sample_fmt,sample_rate,channels,channel_layout,time_base,extradata_hash:format=format_name",
        "-of", "json", spec["source"]], cancelled, timeout=30)
    data = json.loads(text)
    streams = data.get("streams", [])
    if len(streams) != 1 or streams[0].get("codec_type") != "audio":
        raise ValueError("Copy joining needs standalone files with one audio stream and no artwork/video. Extract first or choose Convert audio.")
    return dict(stream=streams[0], format=data.get("format", {}).get("format_name"))


def decoded_samples(spec, cancelled):
    samples = 0
    def count(line):
        nonlocal samples
        value = line.strip().split(",", 1)[0]
        if value:
            if not value.isdecimal():
                raise ValueError("Could not measure decoded audio samples for joining.")
            samples += int(value)
    run_process([spec["tools"]["ffprobe"], "-v", "error", "-protocol_whitelist", "file,pipe", "-threads", "2",
        "-select_streams", str(spec["stream"]["index"]), "-show_frames", "-show_entries", "frame=nb_samples", "-of", "csv=p=0", spec["source"]],
        cancelled, on_line=count)
    if not samples:
        raise ValueError("Selected join input has no decoded audio samples.")
    duration = spec["stream"].get("duration")
    rate = int(spec["stream"]["sample_rate"])
    # Honour a container's valid end boundary instead of retaining the padding
    # of its final compressed frame. Headerless streams use actual decoding.
    return min(samples, round(duration*rate)) if duration and duration > 0 else samples


def reserve_output(parent, name, policy, reserved, cancelled):
    invalid = validate_name(name)
    if invalid:
        raise ValueError(invalid)
    path = parent / name
    count = 1
    while path_key(path) in reserved or os.path.lexists(path):
        if policy == "skip":
            return str(path), "Skipped"
        checkpoint(cancelled)
        path = parent / f"{Path(name).stem}_{count}{Path(name).suffix}"
        if validate_name(path.name) or count > 100000:
            raise ValueError("Output name is too long or has too many conflicts.")
        count += 1
    reserved.add(path_key(path))
    return str(path), "Planned"


def plan_edits(records, request, cancelled=lambda: False):
    from .audio import plan_conversion
    if request.audio_action not in ("convert", "trim", "split", "join"):
        raise ValueError("Choose Convert / extract, Trim, Split or Join.")
    ranges = parse_ranges(request)
    # Inspect every source even when the intermediate, unsuffixed name exists.
    base = plan_conversion(records, replace(request, collision="unique"), cancelled)
    if any(i.status not in ("Planned", "Skipped") for i in base.items):
        return replace(base, request=request)
    active = [i for i in base.items if i.status == "Planned"]
    if request.audio_mode == "copy" and request.audio_action in ("trim", "split", "join") and request.extension not in ("mp3", "m4a"):
        raise ValueError("Copy editing supports MP3 or M4A/AAC. Choose Convert audio for WAV, FLAC or Opus edits.")
    reserved = {path_key(r.path) for r in records if not r.sample}
    if request.audio_action == "join":
        if not 2 <= len(active) <= MAX_JOIN or len({i.source.path for i in active}) != len(active):
            raise ValueError(f"Join needs 2–{MAX_JOIN} different inputs and exactly one selected audio track per input.")
        if request.audio_join_order:
            order = tuple(request.audio_join_order)
            if len(order) != len(active) or len(set(order)) != len(order) or set(order) != {i.source.path for i in active}:
                raise ValueError("The selected inputs changed. Review Join order again.")
            active.sort(key=lambda i: order.index(i.source.path))
        sources = [i.audio_spec for i in active]
        settings = sources[0]["settings"]
        if any(s["settings"] != settings for s in sources):
            raise ValueError("Join inputs need a common sample rate and channel layout. Choose an explicit Sample rate and Mono/Stereo, or compatible inputs.")
        if request.audio_mode == "copy":
            signatures = [copy_signature(s, cancelled) for s in sources]
            if any(s != signatures[0] for s in signatures):
                raise ValueError("Copy join needs matching containers, codec parameters and time bases. Choose Convert audio with a common preset.")
            for source in sources:
                source["copy_packets"] = packet_scan(source["source"], source["stream"], source["tools"], cancelled)
            duration = sum(s["copy_packets"]["duration"] for s in sources)
        else:
            for source in sources:
                source["join_samples"] = decoded_samples(source, cancelled)
                source["duration"] = source["join_samples"] / int(source["stream"]["sample_rate"])
            duration = sum(s["duration"] for s in sources)
        edit = dict(action="join", **fades(request, duration))
        spec = dict(sources[0], sources=sources, duration=duration, edit=edit)
        first = active[0]
        output, status = reserve_output(Path(spec["parent"]), f"{Path(first.source.name).stem}_joined.{request.extension}", request.collision, reserved, cancelled)
        spec.update(output=output, name=f"Join {len(sources)} inputs", track=1)
        if request.audio_mode == "copy":
            join_manifest(spec)
        if len(subprocess.list2cmdline(edit_command(spec, Path(spec["parent"]) / "preview.part")).encode("utf-16-le")) // 2 > 30000:
            raise ValueError("Join command is too long for Windows. Choose fewer inputs or shorter paths.")
        reason = f"Join {len(sources)} inputs → {duration:.3f} s. Order: " + " → ".join(Path(s["source"]).name for s in sources)
        if edit["fade_in"] or edit["fade_out"]:
            reason += f". Fades {edit['fade_in']:g} / {edit['fade_out']:g} s"
        if request.audio_mode == "copy":
            reason += ". Encoded packets copied; encoder padding may remain at joins."
        return OperationPlan(request, (replace(first, output=output, status=status, audio_spec=spec,
            reason=reason if status == "Planned" else "Joined output already exists."),), (reason,))
    items = [i for i in base.items if i.status != "Planned"]
    for item in active:
        try:
            for index, (start, requested_end) in enumerate(ranges, 1):
                original = item.audio_spec
                end = original["duration"] if requested_end is None else requested_end
                if start < 0 or end <= start or end > original["duration"] + .000001:
                    raise ValueError(f"Range {index} must lie within this track's {original['duration']:.3f} seconds and end after its start.")
                if round(end*int(original["stream"]["sample_rate"])) <= round(start*int(original["stream"]["sample_rate"])):
                    raise ValueError("The selected range is shorter than one audio sample.")
                edit = dict(action=request.audio_action, start=start, end=end)
                duration = end-start
                spec = dict(original)
                if request.audio_mode == "copy":
                    summary = packet_scan(spec["source"], spec["stream"], spec["tools"], cancelled, start, end)
                    edit.update(start=summary["first"], end=summary["end"], packet_origin=summary["origin"], copy_packets=summary)
                    duration = summary["duration"]
                edit.update(fades(request, duration))
                spec.update(edit=edit, duration=duration)
                suffix = f"_track{spec['track']:02d}" if len(spec["input_info"]["streams"]) > 1 else ""
                suffix += f"_part{index:02d}" if request.audio_action == "split" else "_trim" if request.audio_action == "trim" else "_fade"
                output, status = reserve_output(Path(spec["parent"]), f"{Path(item.source.name).stem}{suffix}.{request.extension}", request.collision, reserved, cancelled)
                spec.update(output=output, name=f"{item.source.name} / Track {spec['track']}" + (f" / Part {index}" if request.audio_action == "split" else ""))
                reason = f"Track {spec['track']}: {edit['start']:.3f}–{edit['end']:.3f} s → {duration:.3f} s"
                if request.audio_mode == "copy":
                    reason += f"; packet boundaries (requested {start:.3f}–{end:.3f} s)"
                if edit["fade_in"] or edit["fade_out"]:
                    reason += f"; fades {edit['fade_in']:g} / {edit['fade_out']:g} s"
                items.append(replace(item, output=output, status=status, reason=reason if status == "Planned" else "Output already exists.", audio_spec=spec))
        except Exception as error:
            if cancelled():
                raise PlanCancelled() from error
            # An invalid later split blocks the entire snapshot, never a partial
            # silent range selection. Earlier preview rows remain inspectable.
            items.append(replace(item, output=None, status="Invalid", reason=str(error), audio_spec=None))
    return OperationPlan(request, tuple(items))


def fade_filters(edit, duration):
    filters = []
    if edit["fade_in"]:
        filters.append(f"afade=t=in:st=0:d={edit['fade_in']:.6f}")
    if edit["fade_out"]:
        filters.append(f"afade=t=out:st={duration-edit['fade_out']:.6f}:d={edit['fade_out']:.6f}")
    return filters


def edit_command(spec, temp):
    from .audio import encode_command
    plain = dict(spec)
    plain.pop("edit")
    args = encode_command(plain, temp)
    edit = spec["edit"]
    if edit["action"] == "join" and spec["mode"] == "copy":
        index = args.index("-i")
        args[index:index+2] = ["-f", "concat", "-safe", "0", "-i", "pipe:0"]
        args[args.index("-map")+1] = "0:a:0"
        return args
    if spec["mode"] == "copy":
        index = args.index("-i")
        args[index:index] = ["-copyts", "-itsoffset", f"{-edit['packet_origin']:.9f}"]
        index = args.index("-map")
        args[index:index] = ["-ss", f"{edit['start']:.9f}", "-t", f"{spec['duration']:.9f}"]
        return args
    filter_index = args.index("-af")
    if edit["action"] != "join":
        rate = int(spec["stream"]["sample_rate"])
        filters = ["asetpts=N/SR/TB", f"atrim=start_sample={round(edit['start']*rate)}:end_sample={round(edit['end']*rate)}", "asetpts=N/SR/TB"]
        filters += fade_filters(edit, spec["duration"])
        args[filter_index+1] = ",".join(filters)
        return args
    del args[filter_index:filter_index+2]
    input_index = args.index("-i")
    del args[input_index:input_index+2]
    inputs, graph, labels = [], [], []
    settings = spec["settings"]
    for index, source in enumerate(spec["sources"]):
        inputs += ["-threads", "2", "-protocol_whitelist", "file,pipe", "-i", source["source"]]
        graph.append(f"[{index}:{source['stream']['index']}]atrim=end_sample={source['join_samples']},asetpts=N/SR/TB,aresample={settings['sample_rate']},aformat=sample_fmts=fltp:channel_layouts={settings['channel_layout']}[a{index}]")
        labels.append(f"[a{index}]")
    graph.append("".join(labels) + f"concat=n={len(labels)}:v=0:a=1" +
                 ("," + ",".join(fade_filters(edit, spec["duration"])) if edit["fade_in"] or edit["fade_out"] else "") + "[joined]")
    args[input_index:input_index] = inputs + ["-filter_complex_threads", "1", "-filter_complex", ";".join(graph)]
    args[args.index("-map")+1] = "[joined]"
    return args


def join_manifest(spec):
    lines = ["ffconcat version 1.0"]
    for source in spec["sources"]:
        path = str(Path(source["source"]).resolve(strict=True))
        if os.name == "nt":
            path = path.replace("\\", "/")
        if "\n" in path or "\r" in path or "\0" in path:
            raise ValueError("This filename cannot be represented safely in a join manifest.")
        quoted = ("file:" + path).replace("'", "'\\''")
        lines.append(f"file '{quoted}'")
        lines.append(f"duration {source['copy_packets']['duration']:.9f}")
    manifest = "\n".join(lines) + "\n"
    if len(manifest.encode("utf-8")) > 65536:
        raise ValueError("Join manifest is too long. Use fewer inputs.")
    return manifest


def verify_copy_edit(spec, temp, actual, cancelled):
    messages, stop, errors = queue.Queue(maxsize=32), threading.Event(), []
    joining = spec["edit"]["action"] == "join"
    def stopped():
        return stop.is_set() or cancelled()
    def send(packet):
        while True:
            checkpoint(stopped)
            try:
                messages.put(packet, timeout=.1)
                return
            except queue.Full:
                pass
    def produce():
        offset = 0.
        try:
            for source in source_specs(spec):
                check_source(source)
                edit = spec["edit"]
                expected = source["copy_packets"] if joining else edit["copy_packets"]
                def collect(packet):
                    send(dict(packet, pts=packet["pts"]-expected["first"]+offset,
                              dts=packet["dts"]-expected["first"]+offset))
                summary = packet_scan(source["source"], source["stream"], spec["tools"], stopped,
                    None if joining else edit["start"], None if joining else edit["end"]-1e-6, on_packet=collect)
                if summary["digest"] != expected["digest"] or summary["count"] != expected["count"]:
                    raise ValueError("Source packets changed after preview.")
                offset += summary["duration"]
            send(None)
        except Exception as error:
            errors.append(error)
    producer = threading.Thread(target=produce, name="audio-edit-packets", daemon=True)
    producer.start()
    def receive():
        while True:
            checkpoint(cancelled)
            if errors:
                raise errors[0]
            try:
                return messages.get(timeout=.1)
            except queue.Empty:
                if not producer.is_alive():
                    raise ValueError("Source packet inspection stopped unexpectedly.")
    first = None
    def compare(packet):
        nonlocal first
        source = receive()
        if source is None or source["hash"] != packet["hash"]:
            raise ValueError("Copy edit changed or lost encoded packets. Choose Convert audio.")
        if first is None:
            first = packet["pts"]
        tolerance = .002 if not joining else .002 * len(spec["sources"])
        if any(abs(packet[key]-first-source[key]) > tolerance for key in ("pts", "dts")):
            raise ValueError("Copied packet timing differs from the preview. Output was not committed.")
    try:
        output = packet_scan(temp, actual, spec["tools"], cancelled, on_packet=compare)
        if receive() is not None:
            raise ValueError("Copy edit lost encoded packets. Output was not committed.")
        tolerance = .004 if not joining else .004 * len(spec["sources"])
        if abs(output["duration"] - spec["duration"]) > tolerance:
            raise ValueError("Copied packet duration differs from the preview. Output was not committed.")
    finally:
        stop.set()
        producer.join(timeout=10)
        if producer.is_alive():
            raise ValueError("Source packet inspection did not stop cleanly.")
