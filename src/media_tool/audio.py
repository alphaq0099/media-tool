"""Audio conversion/extraction with bounded probes and verified no-replace outputs."""
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import uuid

from .domain import path_key
from .images import check_parent
from .jobs import (check_source, checkpoint, cleanup_temp, commit_no_replace, hash_file,
                   identity, owns_temp, regular)
from .planning import (OperationPlan, PlanCancelled, build_plan, validate_name)
from .processes import run_process
from .tool_paths import resolve_tool
from .video import check_tools, number, probe
from .video_tracks import choose

# encoder, muxer, decoded codec; WAV uses 24-bit PCM for encoded output.
FORMATS = {"mp3": ("libmp3lame", "mp3", "mp3"), "m4a": ("aac", "ipod", "aac"),
           "flac": ("flac", "flac", "flac"), "wav": ("pcm_s24le", "wav", "pcm_s24le"),
           "opus": ("libopus", "opus", "opus")}
COPY_CODECS = {"mp3": {"mp3"}, "m4a": {"aac"}, "flac": {"flac"},
               "wav": {"pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le"}, "opus": {"opus"}}
RATES = (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000, 88200, 96000, 192000)


def inspect_tools(extension="mp3", copy=False, cancelled=lambda: False):
    if extension not in FORMATS:
        raise ValueError("Choose MP3, M4A, FLAC, WAV or Opus.")
    tools = {}
    for name in ("ffmpeg", "ffprobe"):
        path = resolve_tool(name)
        if not path:
            raise ValueError("FFmpeg and FFprobe are required. Put them on PATH or in the app's tools folder.")
        tools[name] = str(Path(path).resolve(strict=True))
        tools[name + "_identity"] = identity(regular(tools[name]))
    encoder, muxer, _ = FORMATS[extension]
    if not copy:
        encoders = run_process([tools["ffmpeg"], "-hide_banner", "-encoders"], cancelled, timeout=15)
        if not re.search(r"^\s*A[.A-Z]{5}\s+" + encoder + r"\s", encoders, re.M):
            raise ValueError(f"This FFmpeg build has no {encoder} encoder for {extension.upper()}.")
    muxers = run_process([tools["ffmpeg"], "-hide_banner", "-muxers"], cancelled, timeout=15)
    if not re.search(r"^\s*E\s+" + muxer + r"\s", muxers, re.M):
        raise ValueError(f"This FFmpeg build cannot write {extension.upper()} ({muxer} muxer missing).")
    return tools


def audio_info(path, tools, cancelled=lambda: False):
    info = probe(path, tools, cancelled, audio_layout=True, track_details=True)
    return {"streams": [s for s in info["streams"] if s["codec_type"] == "audio"],
            "duration": info["duration"], "format": info["format"]}


def packet_bounds(path, stream, tools, cancelled):
    """Measure only the chosen track when its stream header has no duration."""
    first, last, count = None, None, 0
    def collect(line):
        nonlocal first, last, count
        fields = dict(v.split("=", 1) for v in line.split("|") if "=" in v)
        pts, duration = number(fields.get("pts_time")), number(fields.get("duration_time"))
        if pts is not None:
            first = pts if first is None else min(first, pts)
            last = max(last if last is not None else pts, pts + max(duration or 0, 0))
            count += 1
    run_process([tools["ffprobe"], "-v", "error", "-protocol_whitelist", "file,pipe", "-select_streams", str(stream["index"]),
                 "-show_packets", "-show_entries", "packet=pts_time,duration_time", "-of", "compact=p=0:nk=0", str(path)],
                cancelled, on_line=collect)
    if not count or last is None or first is None or last <= first:
        raise ValueError("Selected audio has no measurable duration.")
    return last - first, first


def packet_duration(path, stream, tools, cancelled):
    return packet_bounds(path, stream, tools, cancelled)[0]


def validate_request(request):
    if request.audio_mode not in ("encode", "copy") or request.audio_tracks_mode not in ("all", "single", "custom"):
        raise ValueError("Choose an audio operation and track selection.")
    if request.audio_channels not in ("preserve", "mono", "stereo"):
        raise ValueError("Choose original channels, mono or stereo.")
    if request.audio_sample_rate not in (0, *RATES):
        raise ValueError("Choose a supported sample rate.")
    if request.audio_rate_mode not in ("quality", "bitrate") or request.audio_quality not in range(10):
        raise ValueError("Choose an audio quality or bitrate mode.")
    if not 32 <= request.audio_bitrate <= 320:
        raise ValueError("Audio bitrate must be 32–320 kb/s.")
    if request.audio_mode == "copy" and (request.audio_sample_rate or request.audio_channels != "preserve"):
        raise ValueError("Stream copy cannot change sample rate or channels. Choose Automatic and Original channels.")


def output_settings(stream, request):
    extension = request.extension
    source_rate, source_channels = int(stream["sample_rate"] or 0), stream["channels"] or 0
    if source_rate <= 0 or not 1 <= source_channels <= 8:
        raise ValueError("Selected track needs a known sample rate and between one and eight channels.")
    if request.audio_mode == "copy":
        if stream["codec_name"] not in COPY_CODECS[extension]:
            raise ValueError(f"Cannot copy {stream['codec_name']} into {extension.upper()}. Choose Convert or a compatible format.")
        channels, rate = source_channels, source_rate
        codec = stream["codec_name"]
    else:
        channels = {"mono": 1, "stereo": 2}.get(request.audio_channels, source_channels)
        rate = request.audio_sample_rate or source_rate
        codec = FORMATS[extension][2]
        if extension == "mp3":
            if channels > 2:
                raise ValueError("MP3 supports mono or stereo. Choose Stereo to downmix this track.")
            if not request.audio_sample_rate and rate > 48000:
                rate = 48000
            if rate not in RATES[:9]:
                raise ValueError("MP3 needs a sample rate between 8 and 48 kHz from the list.")
        if extension == "opus":
            if request.audio_sample_rate not in (0, 48000):
                raise ValueError("Opus output uses 48 kHz. Choose Automatic or 48000 Hz.")
            rate = 48000
        if extension == "m4a" and rate > 96000:
            if request.audio_sample_rate:
                raise ValueError("AAC output supports sample rates up to 96000 Hz.")
            rate = 96000
    layout = "mono" if channels == 1 else "stereo" if channels == 2 else stream.get("channel_layout")
    if not layout or layout == "unknown":
        raise ValueError("Unknown multichannel layout; choose Mono or Stereo before converting.")
    return dict(codec=codec, sample_rate=rate, channels=channels, channel_layout=layout)


def plan_audio(records, request, cancelled=lambda: False):
    if request.audio_action == "tags":
        from .audio_tags import plan_tags
        return plan_tags(tuple(records), request, cancelled)
    if request.audio_action in ("volume", "normalize"):
        from .audio_levels import plan_levels
        return plan_levels(tuple(records), request, cancelled)
    if request.audio_action != "convert" or request.audio_fade_in or request.audio_fade_out:
        from .audio_edit import plan_edits
        return plan_edits(tuple(records), request, cancelled)
    return plan_conversion(records, request, cancelled)


def plan_conversion(records, request, cancelled=lambda: False):
    validate_request(request)
    records = tuple(records)
    base = build_plan(records, request, cancelled)
    if request.destination.strip() and not Path(request.destination).expanduser().is_dir():
        raise ValueError("Choose an existing output folder before converting audio.")
    tools = inspect_tools(request.extension, request.audio_mode == "copy", cancelled)
    reserved = {path_key(r.path) for r in records if not r.sample}
    items = []
    for item in base.items:
        if cancelled():
            raise PlanCancelled()
        # Replan names below: a colliding unsuffixed base must not skip all tracks.
        if item.status not in ("Planned", "Skipped") or item.reason == "Duplicate input path.":
            items.append(item)
            continue
        try:
            source = item.source
            spec = dict(source=source.path, identity=[source.device, source.inode, source.size, source.modified_ns])
            check_source(spec)
            info = audio_info(source.path, tools, cancelled)
            streams = info["streams"]
            tracks = choose(streams, request.audio_tracks_mode, request.audio_tracks, "Audio")
            if not tracks:
                raise ValueError("This file has no audio tracks.")
            parent = (Path(request.destination).expanduser() if request.destination.strip() else Path(source.path).parent).resolve(strict=True)
            if not parent.is_dir() or not parent.stat().st_ino:
                raise ValueError("Choose an existing output folder with stable file identities.")
            pending = []
            local_reserved = set(reserved)
            for stream in tracks:
                checkpoint(cancelled)
                ordinal = streams.index(stream) + 1
                settings = output_settings(stream, request)
                suffix = f"_track{ordinal:02d}" if len(streams) > 1 else ""
                name = f"{Path(source.name).stem}{suffix}.{request.extension}"
                invalid = validate_name(name)
                if invalid:
                    raise ValueError(invalid)
                output = parent / name
                counter = 1
                while path_key(output) in local_reserved or os.path.lexists(output):
                    if request.collision == "skip":
                        break
                    checkpoint(cancelled)
                    output = parent / f"{Path(name).stem}_{counter}.{request.extension}"
                    if validate_name(output.name) or counter > 100000:
                        raise ValueError("Too many filename conflicts or output name too long.")
                    counter += 1
                if request.collision == "skip" and (path_key(output) in local_reserved or os.path.lexists(output)):
                    pending.append(replace(item, output=str(output), status="Skipped", reason=f"Track {ordinal}: output already exists."))
                    continue
                duration = stream["duration"]
                if duration is None or duration <= 0:
                    duration = packet_duration(source.path, stream, tools, cancelled)
                audio_spec = dict(spec, output=str(output), parent=str(parent), parent_identity=identity(parent.stat())[:2],
                    name=f"{source.name} / Track {ordinal}", extension=request.extension, tools=tools, input_info=info, stream=stream,
                    track=ordinal, duration=duration, mode=request.audio_mode, rate_mode=request.audio_rate_mode,
                    quality=request.audio_quality, bitrate=request.audio_bitrate, settings=settings, keep_originals=True)
                reason = (f"Track {ordinal}: {stream['codec_name']} → {settings['codec']}; "
                          f"{settings['sample_rate']} Hz; {settings['channel_layout']}; {duration:.2f} s")
                if stream.get("language") or stream.get("title"):
                    reason += "; " + " / ".join(s for s in (stream.get("language"), stream.get("title")) if s)
                reason += "; encoded audio copied" if request.audio_mode == "copy" else ""
                pending.append(replace(item, output=str(output), status="Planned", reason=reason, audio_spec=audio_spec))
                local_reserved.add(path_key(output))
            check_source(spec)
            reserved = local_reserved
            items.extend(pending)
        except Exception as error:
            if cancelled():
                raise PlanCancelled() from error
            items.append(replace(item, output=None, status="Invalid", reason=str(error), audio_spec=None))
    return OperationPlan(request, tuple(items))


def audio_snapshot(plan):
    if plan.request.operation != "audio" or any(i.status not in ("Planned", "Skipped") for i in plan.items):
        raise ValueError("Remove invalid or unsupported inputs, then preview again. No files changed.")
    if not any(i.status == "Planned" for i in plan.items):
        raise ValueError("There are no audio tracks to process.")
    specs = []
    for item in plan.items:
        if item.status == "Planned":
            if item.audio_spec is None:
                raise ValueError("Inspect audio before queuing conversion.")
            # Freeze at the public queue boundary, just as Store.enqueue does.
            specs.append(json.loads(json.dumps(item.audio_spec)))
        else:
            specs.append(dict(source=item.source.path, output=item.output,
                name=item.source.name,
                identity=[item.source.device, item.source.inode, item.source.size, item.source.modified_ns],
                initial_state="skipped", initial_message=item.reason))
    return tuple(specs)


def encode_command(spec, temp):
    if spec.get("levels"):
        from .audio_levels import level_command
        return level_command(spec, temp)
    if spec.get("edit"):
        from .audio_edit import edit_command
        return edit_command(spec, temp)
    encoder, muxer, _ = FORMATS[spec["extension"]]
    args = [spec["tools"]["ffmpeg"], "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-nostats", "-y",
            "-threads", "2", "-protocol_whitelist", "file,pipe", "-i", spec["source"],
            "-map", f"0:{spec['stream']['index']}", "-vn", "-sn", "-dn", "-map_metadata", "-1",
            "-map_metadata:s:a", "-1", "-map_chapters", "-1"]
    if spec["mode"] == "copy":
        args += ["-c:a", "copy", "-avoid_negative_ts", "make_zero"]
    else:
        settings = spec["settings"]
        args += ["-c:a", encoder, "-threads:a", "2", "-ar", str(settings["sample_rate"]),
                 "-ac", str(settings["channels"]), "-channel_layout", settings["channel_layout"],
                 "-af", "asetpts=PTS-STARTPTS"]
        if spec["extension"] == "mp3" and spec["rate_mode"] == "quality":
            args += ["-q:a", str(spec["quality"])]
        elif spec["extension"] in ("mp3", "m4a", "opus"):
            args += ["-b:a", f"{spec['bitrate']}k"]
        elif spec["extension"] == "flac":
            args += ["-sample_fmt", "s32", "-compression_level", "5"]
    return args + ["-progress", "pipe:1", "-f", muxer, str(temp)]


def verify_output(spec, info):
    if len(info["streams"]) != 1:
        raise ValueError("Output must contain exactly the selected audio track.")
    actual, expected = info["streams"][0], spec["settings"]
    formats = {"mp3": "mp3", "m4a": "mov", "flac": "flac", "wav": "wav", "opus": "ogg"}
    if formats[spec["extension"]] not in info["format"].split(","):
        raise ValueError("Output audio container failed verification.")
    if (actual["codec_name"] != expected["codec"] or int(actual["sample_rate"] or 0) != expected["sample_rate"]
            or actual["channels"] != expected["channels"] or actual.get("channel_layout") != expected["channel_layout"]):
        raise ValueError("Output audio codec, sample rate or channel layout failed verification.")
    return actual


def run_audio(store, item, cancelled, progress, hook=lambda point: None):
    spec = json.loads(item["spec"])
    parent, output = check_parent(spec), Path(spec["output"])
    check_tools(spec["tools"])
    if item["digest"] and owns_temp(item, output):
        if hash_file(output, cancelled) != item["digest"]:
            raise ValueError("Previously committed audio changed; it was left untouched.")
        cleanup_temp(store, item["id"])
        return "Recovered verified audio output. Original kept."
    if item["temp"] and os.path.lexists(item["temp"]) and not cleanup_temp(store, item["id"]):
        raise ValueError("Temporary path belongs to another file; it was left untouched.")
    checkpoint(cancelled)
    from .audio_edit import source_specs
    sources = source_specs(spec)
    for source in sources:
        check_source(source)
    if os.path.lexists(output):
        raise FileExistsError("Output appeared after preview; existing file was kept.")
    source_digests = []
    for source in sources:
        source_digests.append(hash_file(source["source"], cancelled))
        if audio_info(source["source"], spec["tools"], cancelled) != source["input_info"]:
            raise ValueError("Audio details changed after preview. Add the source again.")
    temp = parent / f".mediatool-{item['job_id']}-{item['id']}-{uuid.uuid4().hex}.part"
    with temp.open("xb") as handle:
        stat = os.fstat(handle.fileno())
        store.set_item(item["id"], "running", "Processing selected audio track", temp=str(temp),
                       temp_dev=str(stat.st_dev), temp_ino=str(stat.st_ino), digest=None)
    hook("created")
    def report(line):
        if line.startswith("out_time_us="):
            value = number(line.partition("=")[2])
            if value is not None:
                from .activity import media
                media(item,value/1_000_000,spec['duration'],'Copying audio' if spec['mode']=='copy' else 'Encoding audio')
            if value is not None:
                progress(min(85, 5 + round(80 * max(0, value) / 1_000_000 / spec["duration"])), 100)
        hook("progress")
    if not owns_temp(store.item(item["id"])):
        raise ValueError("Temporary output ownership changed before encoding.")
    if spec.get("edit", {}).get("action") == "join" and spec["mode"] == "copy":
        from .audio_edit import join_manifest
        run_process(encode_command(spec, temp), cancelled, on_line=report, input_text=join_manifest(spec))
    else:
        run_process(encode_command(spec, temp), cancelled, on_line=report)
    hook("written")
    checkpoint(cancelled)
    if not owns_temp(store.item(item["id"])):
        raise ValueError("Temporary audio was replaced; it was left untouched.")
    if spec.get("tags"):
        from .audio_tags import apply_tags
        owned=store.item(item['id'])
        apply_tags(spec, temp, cancelled, (owned['temp_dev'],owned['temp_ino']))
        if not owns_temp(store.item(item["id"])):
            raise ValueError("Temporary audio ownership changed while writing tags.")
    with temp.open("rb+") as handle:
        os.fsync(handle.fileno())
    store.set_item(item["id"], "verifying", "Decoding audio and checking original SHA-256")
    info = audio_info(temp, spec["tools"], cancelled)
    actual = verify_output(spec, info)
    if spec.get("tags"):
        from .audio_tags import verify_tags
        verify_tags(spec, temp, cancelled)
    level_notice = ""
    if spec.get("levels"):
        from .audio_levels import verify_levels
        level_notice = verify_levels(spec, temp, cancelled)
    actual_duration = actual["duration"] or packet_duration(temp, actual, spec["tools"], cancelled)
    if abs(actual_duration - spec["duration"]) > max(.25, 2048 / spec["settings"]["sample_rate"]):
        raise ValueError("Output audio duration differs from the selected source track.")
    if spec.get("edit") and spec["mode"] == "copy":
        from .audio_edit import verify_copy_edit
        verify_copy_edit(spec, temp, actual, cancelled)
    elif spec["mode"] == "copy":
        from .remux import verify_packet_stream
        source_start = packet_bounds(spec["source"], spec["stream"], spec["tools"], cancelled)[1]
        output_start = packet_bounds(temp, actual, spec["tools"], cancelled)[1]
        verify_packet_stream(spec, temp, spec["stream"], actual, source_start, output_start, cancelled)
    run_process([spec["tools"]["ffmpeg"], "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-threads", "2",
                 "-err_detect", "explode", "-protocol_whitelist", "file,pipe", "-i", str(temp), "-map", "0:a:0", "-f", "null", "-"],
                cancelled, on_line=lambda line: None)
    progress(95, 100)
    digest = hash_file(temp, cancelled)
    for source, original_digest in zip(sources, source_digests):
        if hash_file(source["source"], cancelled) != original_digest:
            raise ValueError("Original changed during processing. Output was not committed.")
        check_source(source)
    verified = identity(regular(temp))
    if not owns_temp(store.item(item['id'])):
        raise ValueError('Temporary audio ownership changed during verification; file was kept.')
    store.set_item(item["id"], "committing", "Verified audio; committing output", digest=digest, source_digest=source_digests[0])
    hook("prepared")
    checkpoint(cancelled)
    check_parent(spec)
    for source in sources:
        check_source(source)
    if identity(regular(temp)) != verified:
        raise ValueError("Temporary audio changed after verification.")
    commit_no_replace(temp, output)
    hook("committed")
    progress(100, 100)
    settings = spec["settings"]
    label = f"Joined {len(sources)} inputs" if len(sources) > 1 else f"Track {spec['track']}"
    original_notice = "All source SHA-256 hashes unchanged." if len(sources) > 1 else "Original SHA-256 unchanged."
    return (f"{label}: {settings['codec']}; {settings['sample_rate']} Hz; {settings['channel_layout']}; "
            f"{actual_duration:.2f} s. {original_notice}" + level_notice)
