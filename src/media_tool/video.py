"""Inspected SDR video conversion with explicit stream selection and verified commits."""
from dataclasses import replace
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import re
import uuid

from .jobs import (Cancelled, check_source, checkpoint, cleanup_temp, commit_no_replace,
                   hash_file, identity, owns_temp, regular)
from .planning import build_plan, OperationPlan, PlanCancelled
from .processes import run_process
from .tool_paths import resolve_tool
from .images import check_parent
from .video_geometry import display_dimensions, display_label_size, video_dimensions
from .resizing import validate_resize
from .compression import size_budget, budget_description, lower_video_rate, measured_size_message
from .video_stats import create_scope, check_scope, cleanup_scope
from .video_formats import output_format, validate_mode, HEVC_QUALITY, VP9_QUALITY, VP9_SPEED, opus_settings, verify_webm_header, CODECS, CONTAINERS
from .video_tracks import selected_tracks, track_specs, audio_specs, label_args, verify_labels, verify_subtitles, track_numbers
from .external_subtitles import prepare_external, check_external, verify_external
from .remux import remux_tracks, remux_command, verify_remux_output, verify_remux_packets

PIXELS = {"yuv420p", "yuvj420p", "nv12", "yuv422p", "yuvj422p", "yuv444p", "yuvj444p",
          "yuyv422", "uyvy422", "rgb24", "bgr24", "gray"}


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def inspect_tools(cancelled=lambda: False, codec="h264", container="mp4", remux=False):
    if remux:
        if container not in CONTAINERS:
            raise ValueError("Choose MP4, MKV or WebM for remuxing.")
        selected, muxer = None, CONTAINERS[container]
    else:
        selected, muxer = output_format(codec, container)
    paths = {name: resolve_tool(name) for name in ("ffmpeg", "ffprobe")}
    if not all(paths.values()):
        raise ValueError("FFmpeg and FFprobe are required. Put them on PATH or in the app's tools folder, then retry.")
    result = {}
    for name, path in paths.items():
        path = str(Path(path).resolve(strict=True))
        result[name] = path
        result[name + "_identity"] = identity(regular(path))
    encoders = "" if remux else run_process([result["ffmpeg"], "-hide_banner", "-encoders"], cancelled, timeout=15)
    if not remux and not re.search(r"^\s*V[.A-Z]{5}\s+" + selected["encoder"] + r"\s", encoders, re.M):
        raise ValueError(f"This FFmpeg build has no {selected['encoder']} encoder. Install a build with {selected['label']} encoding support.")
    muxers = run_process([result["ffmpeg"], "-hide_banner", "-muxers"], cancelled, timeout=15)
    if not re.search(r"^\s*E\s+" + muxer + r"\s", muxers, re.M):
        raise ValueError(f"This FFmpeg build cannot write {container.upper()} ({muxer} muxer missing).")
    result["aac"] = bool(re.search(r"^\s*A[.A-Z]{5}\s+aac\s", encoders, re.M))
    result["opus"] = bool(re.search(r"^\s*A[.A-Z]{5}\s+libopus\s", encoders, re.M))
    return result


def check_tools(tools):
    for name in ("ffmpeg", "ffprobe"):
        if identity(regular(tools[name])) != tools[name + "_identity"]:
            raise ValueError("A media tool changed after preview. Preview the job again.")


def probe(path, tools, cancelled=lambda: False, audio_layout=False, track_details=False, external_timing=False, input_format=None):
    format_args = ["-f", input_format] if input_format else []
    text = run_process([tools["ffprobe"], "-v", "error", "-protocol_whitelist", "file,pipe", *format_args,
                        "-show_streams", "-show_format", "-show_chapters", "-of", "json", str(path)],
                       cancelled, timeout=30)
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as error:
        raise ValueError("FFprobe returned invalid media details.") from error
    streams = []
    for raw in data.get("streams", []):
        stream = {key: raw.get(key) for key in ("index", "codec_type", "codec_name", "pix_fmt", "width", "height",
            "sample_aspect_ratio", "field_order", "bits_per_raw_sample", "color_transfer", "color_primaries",
            "channels", "sample_rate", "avg_frame_rate")}
        stream["duration"] = number(raw.get("duration"))
        stream["start"] = number(raw.get("start_time"))
        stream["attached"] = bool(raw.get("disposition", {}).get("attached_pic"))
        tags = raw.get("tags", {})
        if str(tags.get("alpha_mode", tags.get("ALPHA_MODE", "0"))) == "1":
            stream["alpha"] = True
        stream["language"], stream["title"] = tags.get("language", ""), tags.get("title", "")
        if track_details:
            stream["title"] = tags.get("title", tags.get("handler_name", ""))
            for flag in ("default", "forced"):
                stream[flag] = int(raw.get("disposition", {}).get(flag, 0))
        sides = raw.get("side_data_list", [])
        rotations = [entry.get("rotation", 0) for entry in sides if "rotation" in entry]
        stream["rotation"] = number(rotations[0] if rotations else tags.get("rotate", 0)) or 0
        stream["side_types"] = [entry.get("side_data_type", "") for entry in sides]
        matrices = [entry["displaymatrix"] for entry in sides if "displaymatrix" in entry]
        if matrices:
            stream["display_matrix"] = matrices[0]
        if audio_layout and stream["codec_type"] == "audio":
            stream["channel_layout"] = raw.get("channel_layout")
        streams.append(stream)
    duration = number(data.get("format", {}).get("duration"))
    result = dict(streams=streams, duration=duration if duration and duration > 0 else None,
                chapters=len(data.get("chapters", [])), format=data.get("format", {}).get("format_name", ""))
    if external_timing:
        result["format_start"] = number(data.get("format", {}).get("start_time"))
    return result


def describe(stream):
    text = f"#{stream['index']} {stream['codec_type']} {stream['codec_name']}"
    if stream.get("width"):
        text += f" {stream['width']}×{stream['height']}"
    if stream.get("channels"):
        text += f" {stream['channels']}ch"
    if stream.get("language"):
        text += f" [{stream['language']}]"
    if stream.get("title"):
        text += f" {stream['title']}"
    return text


def packet_durations(path, info, tools, cancelled):
    """Fill missing media durations from per-stream presentation packet spans.

    Matroska often has no stream duration; format duration includes other tracks.
    Keep inspection streaming. Some demuxers omit all sample durations; in
    that case use the observed presentation span as a lower bound, without
    inventing a final sample duration. Verification retains its frame tolerance.
    """
    missing={s['index'] for s in info['streams']
             if s['codec_type'] in ('video','audio') and s.get('duration') is None}
    if not missing:return info
    spans={}
    def receive(line):
        fields=dict(part.split('=',1) for part in line.split('|') if '=' in part)
        try:index=int(fields.get('stream_index','-1'))
        except ValueError:return
        if index not in missing:return
        pts=number(fields.get('pts_time'))
        duration=number(fields.get('duration_time'))
        if pts is None:raise ValueError('Cannot verify a media packet without a presentation timestamp.')
        end=pts+duration if duration is not None and duration>0 else None
        current=spans.setdefault(index,[pts,pts,end])
        current[0]=min(current[0],pts)
        if pts>=current[1]:current[1],current[2]=pts,end
    diagnostics=run_process([tools['ffprobe'],'-v','error','-protocol_whitelist','file,pipe',
        '-show_packets','-show_entries','packet=stream_index,pts_time,duration_time',
        '-of','compact=p=0:nk=0',str(path)],cancelled,on_line=receive,include_stderr=True)
    # FFprobe may emit codec-parser diagnostics for valid Opus packets here;
    # full output decoding is checked separately. Demuxer truncation is fatal
    # even when FFprobe returns zero and yields a shorter packet span.
    if any(marker in diagnostics.lower() for marker in ('file ended prematurely', 'file is truncated', 'packet corrupt', 'invalid data found when processing input')):
        raise ValueError('Media packet inspection reported damaged or incomplete input: '+diagnostics.strip()[-2000:])
    streams=[]
    for stream in info['streams']:
        value=dict(stream)
        if stream['index'] in missing:
            span=spans.get(stream['index'])
            if not span:
                raise ValueError('Cannot verify the final media packet duration; output was not committed.')
            end=span[2] if span[2] is not None else span[1]
            if end<=span[0]:
                raise ValueError('Cannot verify the final media packet duration; output was not committed.')
            value['duration']=end-span[0]
            value['duration_basis']='packet_end' if span[2] is not None else 'presentation_span_lower_bound'
        streams.append(value)
    return dict(info,streams=streams)


def select_streams(info, audio_track):
    videos = [s for s in info["streams"] if s["codec_type"] == "video" and not s["attached"]]
    audios = [s for s in info["streams"] if s["codec_type"] == "audio"]
    if not videos:
        raise ValueError("No ordinary video stream was found.")
    video = videos[0]
    if video.get("alpha") or video["pix_fmt"] not in PIXELS or (number(video["bits_per_raw_sample"]) or 8) > 8:
        raise ValueError("Only 8-bit SDR video is supported in this phase; HDR, high-bit-depth and alpha video are blocked.")
    if (video["color_transfer"] in ("smpte2084", "arib-std-b67") or video["color_primaries"] == "bt2020"
            or any(token in " ".join(video["side_types"]).casefold() for token in ("dovi", "dolby", "mastering", "hdr"))):
        raise ValueError("HDR/wide-gamut video requires a color policy from a later phase.")
    display_dimensions(video)
    if video["field_order"] not in (None, "unknown", "progressive"):
        raise ValueError("Interlaced video is not supported in this phase.")
    if audio_track > len(audios) and not (audio_track == 1 and not audios):
        raise ValueError(f"Audio track {audio_track} is unavailable; this file has {len(audios)} audio tracks.")
    audio = audios[audio_track - 1] if audio_track and audios else None
    if audio and (not audio["channels"] or not 1 <= audio["channels"] <= 6):
        raise ValueError("Selected audio must have between one and six channels.")
    selected = {video["index"]} | ({audio["index"]} if audio else set())
    omitted = [s for s in info["streams"] if s["index"] not in selected]
    duration = video["duration"] or info["duration"]
    return video, audio, omitted, duration


def plan_video(records, request, cancelled=lambda: False):
    if request.video_action not in ("convert", "remux"):
        raise ValueError("Choose Convert video or Remux.")
    remux = request.video_action == "remux"
    if remux:
        if request.video_mode != "quality" or request.resize_mode != "none":
            raise ValueError("Remux copies video/audio unchanged; choose Convert video to resize or set a target size.")
        if request.extension not in CONTAINERS:
            raise ValueError("Choose MP4, MKV or WebM for remuxing.")
        codec = None
    else:
        codec, _ = output_format(request.video_codec, request.extension)
        validate_mode(request.video_codec, request.video_mode == "target_size")
    if not 16 <= request.video_crf <= 30 or request.video_preset not in ("superfast", "veryfast", "medium"):
        raise ValueError("Choose a supported video quality and speed.")
    if not 1 <= request.video_threads <= 8 or not 0 <= request.video_audio <= 99:
        raise ValueError("CPU threads must be 1–8; audio track must be 0–99.")
    validate_resize(request.resize_mode, request.resize_width, request.resize_height, request.resize_percent)
    for mode, text, allowed in ((request.video_audio_mode, request.video_audio_tracks, ("single", "all", "custom", "none")),
                                (request.video_subtitle_mode, request.video_subtitle_tracks, ("all", "custom", "none"))):
        if mode not in allowed:
            raise ValueError("Choose a supported track selection.")
        if mode == "custom":
            track_numbers(text)
    if request.video_mode not in ("quality", "target_size"):
        raise ValueError("Choose Quality or Target file size compression.")
    crf = request.video_crf
    if not remux and request.video_codec in ("hevc", "vp9"):
        qualities = HEVC_QUALITY if request.video_codec == "hevc" else VP9_QUALITY
        if crf not in qualities:
            raise ValueError(f"Choose Low, Balanced, High or Best quality for {codec['label']}.")
        crf = qualities[crf]
    try:
        tools = inspect_tools(cancelled, request.video_codec, request.extension, remux=True) if remux else inspect_tools(cancelled, request.video_codec, request.extension)
        external = prepare_external(request, tools, probe, cancelled)
        plan = build_plan(records, request, cancelled)
        items = []
        for item in plan.items:
            checkpoint(cancelled)
            if item.status != "Planned":
                items.append(item)
                continue
            try:
                parent = Path(item.output).parent.resolve(strict=True)
                spec = dict(source=item.source.path, identity=[item.source.device, item.source.inode, item.source.size, item.source.modified_ns],
                    output=str(parent / Path(item.output).name), parent=str(parent), parent_identity=identity(parent.stat())[:2],
                    name=item.source.name, tools=tools, crf=crf, preset=request.video_preset,
                    codec=request.video_codec, container=request.extension,
                    threads=request.video_threads, audio_track=request.video_audio, keep_originals=request.keep_originals)
                check_source(spec)
                webm = request.extension == "webm"
                info = probe(item.source.path, tools, cancelled, audio_layout=webm or remux, track_details=True, external_timing=external is not None)
                video, _, _, duration = select_streams(info, 0)
                if remux and not {"mov", "matroska", "webm"}.intersection(info["format"].split(",")):
                    raise ValueError("This remux phase supports MP4/MOV, MKV and WebM inputs. Choose Convert video for other containers.")
                audios, subtitles = selected_tracks(info, request)
                selected_ids = {s["index"] for s in [video, *audios, *subtitles]}
                omitted = [s for s in info["streams"] if s["index"] not in selected_ids]
                audio = audios[0] if audios else None
                if remux:
                    spec.update(action="remux", codec=video["codec_name"])
                    spec["audio_tracks"], spec["subtitle_tracks"] = remux_tracks(video, audios, subtitles, request)
                else:
                    spec["audio_tracks"], spec["subtitle_tracks"] = track_specs(audios, subtitles, request)
                if external is not None:
                    if video["start"] is None or info["format_start"] is None or duration is None:
                        raise ValueError("External subtitles need known video start time and duration to verify alignment.")
                    if external["last_end"] > duration + .01:
                        raise ValueError("External subtitles extend beyond this video's duration. Check that you chose the matching subtitle file and timing.")
                    spec["external_subtitle"] = dict(external)
                    spec["external_subtitle"]["default"] = int(not any(track["default"] for track in spec["subtitle_tracks"]))
                    spec["external_offset"] = video["start"] - info["format_start"]
                    spec["subtitle_tracks"].append(dict(spec["external_subtitle"]))
                    check_external(external, cancelled)
                if not remux and audios and not tools["opus" if webm else "aac"]:
                    raise ValueError(f"This FFmpeg build has no {'libopus (Opus)' if webm else 'AAC'} encoder for the selected audio.")
                if not remux and audio and webm:
                    spec["audio_output"] = opus_settings(audio)
                resize = dict(mode=request.resize_mode, width=request.resize_width, height=request.resize_height,
                              percent=request.resize_percent, aspect=request.resize_aspect, no_upscale=request.resize_no_upscale)
                spec.update(video=info, video_index=video["index"], audio_index=audio["index"] if audio else None,
                            output_size=[video["width"], video["height"]] if remux else list(video_dimensions(video, **resize)),
                            display_size=list(display_label_size(video)), duration=duration)
                if not remux:
                    spec["resize"] = resize
                if not remux and request.video_codec == "hevc" and min(spec["output_size"]) < 16:
                    raise ValueError("H.265 needs at least 16 pixels on each output side. Increase the resize bounds or choose H.264.")
                if request.video_mode == "target_size":
                    spec["budget"] = size_budget(request.video_target_size, request.video_target_unit,
                        request.video_audio_kbps, info, video, audios)
                    if request.video_reuse_analysis:
                        spec["reuse_analysis"] = True
                check_source(spec)
                selected = "; ".join(describe(s) for s in [video, *audios, *subtitles]) + ("" if audios else "; no audio")
                losses = [describe(s) for s in omitted] + ([f"{info['chapters']} chapters"] if info["chapters"] else [])
                audio_kbps = spec["budget"]["audio_bitrate"] // 1000 if "budget" in spec else spec.get("audio_output", {}).get("bitrate", 192_000) // 1000
                reason = ("Keep: " + selected + f". Output: {request.extension.upper()} / {CODECS[spec['codec']]['label']}. Video/audio copied without re-encoding; packet content and timing verified. "
                          "Coded dimensions, pixel aspect and supported orientation retained. No target size guarantee." if remux else
                          "Keep: " + selected + f". Output: {request.extension.upper()} / {codec['label']}, 8-bit SDR" + (f", {'Opus' if webm else 'AAC'} {audio_kbps} kb/s." if audio else "."))
                if not remux and audios and webm:
                    for track, sound in zip(audios, spec["audio_tracks"]):
                        reason += f" Audio #{track['index']}: Opus VBR {sound['bitrate'] // 1000} kb/s, 48 kHz; {track['channel_layout']} channels preserved."
                        if track["sample_rate"] != "48000":
                            reason += f" Audio resampled from {track['sample_rate']} Hz."
                reason += " Omit: " + ("; ".join(losses) if losses else "none") + ". Global metadata removed; selected track labels retained."
                if audios and not remux:
                    reason += f" {len(audios)} audio track(s); bitrate is per track. First retained source-default audio is default, otherwise first selected."
                if subtitles:
                    reason += f" {len(subtitles)} subtitle track(s) copied; text, timing and flags verified. Font attachments are not copied."
                if external is not None:
                    reason += (f" Add external subtitle: {Path(external['source']).name}, {external['cue_count']} cues, "
                               f"encoded as {external['codec']}, selectable in your player. Title: {external['title']}. "
                               f"Default: {'yes' if spec['external_subtitle']['default'] else 'no'}. "
                               "Timing is relative to the video start; visible text/timing verified. Styling may vary by format/player.")
                if not remux:
                    reason += (f" Display {spec['display_size'][0]}×{spec['display_size'][1]} → "
                           f"{spec['output_size'][0]}×{spec['output_size'][1]}; square pixels, rotation normalized."
                           " Dimensions round down to even pixels.")
                if not remux and request.video_codec == "hevc":
                    reason += " H.265 uses more processing and needs HEVC playback support. Quality levels are codec-specific."
                if "budget" in spec:
                    reason += " " + budget_description(spec["budget"], spec["output_size"], video["avg_frame_rate"])
                    if spec.get("reuse_analysis"):
                        reason += " Completed analysis may be retained for retry only when source/settings/encoder/logs match; removed on success."
                if duration is None:
                    reason += " Duration unknown; progress will be indeterminate."
                if not request.keep_originals:
                    reason += " Original will move to the Recycle Bin only after this output is verified and saved."
                review = bool(losses) and not request.video_allow_omissions
                if review:
                    reason += " Review Stream details, then enable Allow omitted tracks to continue."
                items.append(replace(item, output=spec["output"], status="Review" if review else "Planned", reason=reason, video_spec=spec))
            except Cancelled:
                raise
            except Exception as error:
                items.append(replace(item, status="Invalid", reason=f"Cannot convert video: {error}"))
        warnings = ["Remux copies selected video/audio without quality loss; container overhead and subtitle changes can change file size. Verification fully reads/decodes the output." if remux else
                    "First ordinary video plus selected audio/subtitles. Track numbers start at 1 separately for audio and subtitles in each file. Inspect Stream details before accepting omitted tracks."]
        if request.video_mode == "target_size":
            warnings.append("Size limit applies to each output. Stream details shows each bitrate budget; Results shows measured size. Tight budgets may need resizing.")
        return OperationPlan(request, tuple(items), tuple(warnings))
    except Cancelled as error:
        raise PlanCancelled() from error


def video_snapshot(plan):
    if plan.request.operation != "video" or any(i.status not in ("Planned", "Skipped") for i in plan.items):
        raise ValueError("Review invalid inputs or omitted tracks in Preview. No video job was started.")
    if not any(i.status == "Planned" for i in plan.items):
        raise ValueError("There are no videos to convert.")
    specs = []
    for item in plan.items:
        if item.status == "Planned":
            if item.video_spec is None:
                raise ValueError("Inspect video details before converting.")
            specs.append(item.video_spec)
        else:
            specs.append(dict(source=item.source.path, output=item.output,
                identity=[item.source.device, item.source.inode, item.source.size, item.source.modified_ns],
                initial_state="skipped", initial_message=item.reason))
    return tuple(specs)


def encode_command(spec, temp, pass_number=0, passlog=None, bitrate=None):
    if spec.get("action") == "remux":
        if pass_number or "budget" in spec:
            raise ValueError("Remux cannot use compression passes or a size budget.")
        return remux_command(spec, temp)
    codec, muxer = output_format(spec.get("codec", "h264"), spec.get("container", "mp4"))
    validate_mode(spec.get("codec", "h264"), bool(pass_number) or "budget" in spec)
    args = [spec["tools"]["ffmpeg"], "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-nostats", "-y",
            "-progress", "pipe:1", "-stats_period", "0.2", "-filter_threads", "1",
            "-threads", str(spec["threads"]), "-protocol_whitelist", "file,pipe", "-err_detect", "explode", "-i", spec["source"]]
    if spec.get("external_subtitle") and pass_number != 1:
        external = spec["external_subtitle"]
        args += ["-protocol_whitelist", "file,pipe", "-itsoffset", str(spec["external_offset"]),
                 "-f", external["input_format"], "-sub_charenc", "UTF-8", "-i", external["source"]]
    args += ["-map", f"0:{spec['video_index']}"]
    sounds = audio_specs(spec) if pass_number != 1 else []
    subtitles = spec.get("subtitle_tracks", []) if pass_number != 1 else []
    for ordinal, sound in enumerate(sounds):
        args += ["-map", f"0:{sound['index']}", f"-c:a:{ordinal}", "libopus" if muxer == "webm" else "aac", f"-b:a:{ordinal}", str(sound["bitrate"])]
        if muxer == "webm":
            args += [f"-ar:a:{ordinal}", str(sound["sample_rate"]), f"-mapping_family:a:{ordinal}", str(sound["mapping_family"]), f"-vbr:a:{ordinal}", "on"]
    if not sounds:
        args += ["-an"]
    for ordinal, subtitle in enumerate(subtitles):
        args += ["-map", f"{subtitle.get('input', 0)}:{subtitle['index']}", f"-c:s:{ordinal}", subtitle["codec"] if subtitle.get("input") else "copy"]
    if not subtitles:
        args += ["-sn"]
    if "resize" in spec:
        w, h = spec["output_size"]
        # FFmpeg autorotates before user filters. Normalize SAR after display-aware scaling.
        args += ["-vf", f"scale={w}:{h}:flags=lanczos,setsar=1"]
    rate_options = (["-b:v", str(bitrate)] if pass_number and spec.get("codec") == "hevc" else
                    ["-b:v", str(bitrate), "-pass", str(pass_number), "-passlogfile", str(passlog), "-fastfirstpass", "0"]
                    if pass_number else ["-crf", str(spec["crf"])])
    speed_options = (["-deadline", "good", "-cpu-used", str(VP9_SPEED[spec["preset"]]), "-b:v", "0",
                      "-row-mt", "1", "-lag-in-frames", "16"] if spec.get("codec") == "vp9" else ["-preset", spec["preset"]])
    args += ["-dn", "-map_metadata", "-1", "-map_metadata:s", "-1", "-map_chapters", "-1", "-c:v", codec["encoder"],
        *speed_options, *rate_options, "-pix_fmt", "yuv420p", "-threads:v", str(spec["threads"]),
        # Match the source clock: the default 1/fps clock can collapse VFR timestamps.
        "-fps_mode", "passthrough", "-enc_time_base:v", "demux"]
    if spec.get("codec") == "hevc":
        # x265 otherwise creates its own auto-sized pool in addition to frame threads.
        args += ["-x265-params", f"pools=none:frame-threads={spec['threads']}:log-level=error"]
        if min(spec["output_size"]) < 64:
            args[-1] += ":ctu=16"
        if pass_number:
            args[-1] += f":pass={pass_number}:stats=x265.log:slow-firstpass=1"
        if muxer == "mp4":
            args += ["-tag:v", "hvc1"]
    if pass_number == 1:
        return args + ["-f", "null", os.devnull]
    if "audio_tracks" in spec:
        for kind, tracks in (("a", sounds), ("s", subtitles)):
            for ordinal, track in enumerate(tracks):
                args += label_args(track, kind, ordinal, spec.get("container", "mp4"))
        if muxer in ("matroska", "webm"):
            args += ["-default_mode", "passthrough"]
    if muxer == "mp4":
        args += ["-movflags", "+faststart"]
    return args + ["-f", muxer, str(temp)]


def encode_target(store, item, spec, temp, cancelled, progress, hook, source_digest=None):
    from .video_checkpoint import cached_analysis, save_analysis
    budget = spec["budget"]
    bitrate = budget["video_bitrate"]
    reuse = spec.get("reuse_analysis", False)
    engine_digest = hash_file(spec["tools"]["ffmpeg"], cancelled) if reuse else None
    cached = cached_analysis(store, item["id"], spec, source_digest, engine_digest, cancelled) if reuse else None
    first_attempt = cached["analysis"]["attempt"] if cached else 0
    if cached:
        bitrate = cached["analysis"]["bitrate"]
    for attempt in range(first_attempt, 2):
        scope = cached if cached else create_scope(store, item["id"], spec.get("codec", "h264"))
        analysis_ready = cached is not None
        try:
            for pass_number in (1, 2):
                if pass_number == 1 and analysis_ready:
                    store.set_item(item["id"], "running", "Reusing verified completed analysis; starting output pass")
                    hook("analysis_reused")
                    continue
                checkpoint(cancelled)
                check_source(spec)
                check_external(spec.get("external_subtitle"), cancelled)
                root = check_scope(store, scope)
                if not owns_temp(store.item(item["id"])):
                    raise ValueError("Temporary video ownership changed before a compression pass.")
                store.set_item(item["id"], "running", f"Target size: attempt {attempt + 1}/2, pass {pass_number}/2")
                hook(f"pass{pass_number}_started")
                def report(line):
                    if line.startswith("out_time_us="):
                        value = number(line.partition("=")[2])
                        if value is not None:
                            from .activity import media
                            media(item,value/1_000_000,budget['duration'],f'Encoding video · pass {pass_number}/2 · attempt {attempt+1}/2')
                            fraction = min(1, max(0, value / 1_000_000 / budget["duration"]))
                            progress(5 + round((attempt * 2 + pass_number - 1 + fraction) * 20), 100)
                    hook("progress")
                command = encode_command(spec, temp, pass_number, root / "stats", bitrate)
                if spec.get("codec") == "hevc":
                    run_process(command, cancelled, on_line=report, cwd=root)
                else:
                    run_process(command, cancelled, on_line=report)
                check_scope(store, scope)
                if pass_number == 1 and reuse:
                    save_analysis(store, item["id"], scope, spec, source_digest, engine_digest, bitrate, attempt, cancelled)
                    analysis_ready = True
                    hook("analysis_saved")
                hook(f"pass{pass_number}_finished")
        finally:
            if not (reuse and analysis_ready):
                cleanup_scope(store, item["id"])
        if not owns_temp(store.item(item["id"])):
            raise ValueError("Temporary video ownership changed after compression.")
        measured = temp.stat().st_size
        hook("size_checked")
        if measured <= budget["target_bytes"]:
            return
        cleanup_scope(store, item["id"])
        cached = None
        corrected = lower_video_rate(budget, measured, bitrate)
        if attempt or corrected >= bitrate:
            break
        store.set_item(item["id"], "running", f"First output {measured:,} bytes exceeds {budget['target_bytes']:,}; one lower-bitrate retry")
        bitrate = corrected
    raise ValueError(f"Target missed: {measured:,} bytes exceeds the {budget['target_bytes']:,}-byte cap after bounded compression. "
                     "Increase the target, lower audio bitrate or resize. No final output was saved; original kept.")


def verify_output(spec, info):
    if spec.get("action") == "remux":
        return verify_remux_output(spec, info)
    _, muxer = output_format(spec.get("codec", "h264"), spec.get("container", "mp4"))
    if muxer not in info["format"].split(","):
        raise ValueError(f"Output container is not {spec.get('container', 'mp4').upper()}.")
    streams = info["streams"]
    videos = [s for s in streams if s["codec_type"] == "video"]
    audios = [s for s in streams if s["codec_type"] == "audio"]
    subtitles = [s for s in streams if s["codec_type"] == "subtitle"]
    sounds = audio_specs(spec)
    if len(videos) != 1 or len(audios) != len(sounds) or len(subtitles) != len(spec.get("subtitle_tracks", [])) or len(streams) != 1 + len(audios) + len(subtitles):
        raise ValueError("Output stream count does not match the saved selection.")
    video = videos[0]
    if video["codec_name"] != spec.get("codec", "h264") or video["pix_fmt"] != "yuv420p" or [video["width"], video["height"]] != spec["output_size"]:
        raise ValueError("Output video codec, pixel format or dimensions failed verification.")
    if video["rotation"] % 360 or video["sample_aspect_ratio"] not in (None, "N/A", "1:1"):
        raise ValueError("Output display orientation or pixel aspect ratio failed verification.")
    original_video = next(s for s in spec["video"]["streams"] if s["index"] == spec["video_index"])
    try:
        frame = float(Fraction(original_video["avg_frame_rate"]))
        tolerance = max(0.25, 2 / frame) if frame > 0 else 0.5
    except (ValueError, TypeError, ZeroDivisionError):
        tolerance = 0.5
    if spec["duration"] is not None:
        actual = video["duration"] or info["duration"]
        if actual is None or abs(actual - spec["duration"]) > tolerance:
            raise ValueError("Output video duration differs from the source; output was not committed.")
    for sound, audio in zip(sounds, audios):
        original_audio = next(s for s in spec["video"]["streams"] if s["index"] == sound["index"])
        if audio["codec_name"] != sound.get("codec", "aac") or audio["channels"] != original_audio["channels"]:
            raise ValueError("Output audio codec or channel count failed verification.")
        if sound["codec"] == "opus" and (audio["sample_rate"] != str(sound["sample_rate"]) or audio.get("channel_layout") != sound["channel_layout"]):
            raise ValueError("Output Opus sample rate or channel layout failed verification.")
        if original_audio["duration"] and audio["duration"] and abs(original_audio["duration"] - audio["duration"]) > tolerance:
            raise ValueError("Output audio duration differs from the selected source track.")
        starts = [video["start"], audio["start"], original_video["start"], original_audio["start"]]
        if all(value is not None for value in starts) and abs((starts[1]-starts[0]) - (starts[3]-starts[2])) > tolerance:
            raise ValueError("Audio/video start offsets changed beyond the verification tolerance.")
        if "audio_tracks" in spec:
            verify_labels(sound, audio)
    for track, subtitle in zip(spec.get("subtitle_tracks", []), subtitles):
        if subtitle["codec_name"] != track["codec"]:
            raise ValueError("Output subtitle codec failed verification.")
        verify_labels(track, subtitle)


def run_video(store, item, cancelled, progress, hook=lambda point: None):
    spec = json.loads(item["spec"])
    remux = spec.get("action") == "remux"
    codec = CODECS[spec["codec"]] if remux else output_format(spec.get("codec", "h264"), spec.get("container", "mp4"))[0]
    if not remux:
        validate_mode(spec.get("codec", "h264"), "budget" in spec)
    if not spec.get("reuse_analysis"):
        cleanup_scope(store, item["id"])
    parent = check_parent(spec)
    output = Path(spec["output"])
    check_tools(spec["tools"])
    if item["digest"] and owns_temp(item, output):
        if hash_file(output, cancelled) != item["digest"]:
            raise ValueError("Previously committed video changed; it was left untouched.")
        cleanup_temp(store, item["id"])
        cleanup_scope(store, item["id"])
        measured = output.stat().st_size
        if "budget" in spec and measured > spec["budget"]["target_bytes"]:
            raise ValueError("Target missed: recovered output exceeds the saved cap.")
        return ("Recovered verified remuxed video. Original kept." if remux else "Recovered verified converted video. Original kept.") + (" " + measured_size_message(spec["budget"], measured) if "budget" in spec else "")
    if item["temp"] and os.path.lexists(item["temp"]) and not cleanup_temp(store, item["id"]):
        raise ValueError("Temporary path belongs to another file; it was left untouched.")
    checkpoint(cancelled)
    check_source(spec)
    if os.path.lexists(output):
        raise FileExistsError("Output appeared after preview; existing file was kept.")
    original_hash = hash_file(spec["source"], cancelled, lambda done, total: progress(round(5*done/max(total, 1)), 100))
    check_external(spec.get("external_subtitle"), cancelled)
    probe_options = dict(audio_layout=spec.get("codec") == "vp9" or remux, track_details="audio_tracks" in spec,
                         external_timing="external_subtitle" in spec)
    inspected = probe(spec["source"], spec["tools"], cancelled, **probe_options)
    if inspected != spec["video"]:
        raise ValueError("Video details changed after preview. Add the source again.")
    temp = parent / f".mediatool-{item['job_id']}-{item['id']}-{uuid.uuid4().hex}.part"
    with temp.open("xb") as handle:
        info = os.fstat(handle.fileno())
        store.set_item(item["id"], "running", "Copying video/audio streams" if remux else f"Encoding {codec['label']} video", temp=str(temp),
                       temp_dev=str(info.st_dev), temp_ino=str(info.st_ino), digest=None)
    hook("created")
    checkpoint(cancelled)
    def report(line):
        if line.startswith("out_time_us="):
            value = number(line.partition("=")[2])
            if value is not None:
                from .activity import media
                media(item,value/1_000_000,spec['duration'],'Copying streams' if spec.get('action')=='remux' else 'Encoding video')
            if value is not None:
                progress(min(85, 5 + round(80 * max(0, value / 1_000_000) / spec["duration"])), 100) if spec["duration"] else progress(0, 0)
        hook("progress")
    if not owns_temp(store.item(item["id"])):
        raise ValueError("Temporary output ownership changed before encoding.")
    check_external(spec.get("external_subtitle"), cancelled)
    if "budget" in spec:
        encode_target(store, item, spec, temp, cancelled, progress, hook, original_hash)
    else:
        run_process(encode_command(spec, temp), cancelled, on_line=report)
    hook("written")
    checkpoint(cancelled)
    if not owns_temp(store.item(item["id"])):
        raise ValueError("Temporary video was replaced; it was left untouched.")
    with temp.open("rb+") as handle:
        os.fsync(handle.fileno())
    store.set_item(item["id"], "verifying", "Inspecting and fully decoding output; verifying original SHA-256")
    inspected = probe(temp, spec["tools"], cancelled, **probe_options)
    verification_spec=spec
    if not remux:
        inspected=packet_durations(temp,inspected,spec['tools'],cancelled)
        original_video=next(s for s in spec['video']['streams'] if s['index']==spec['video_index'])
        if original_video.get('duration') is None:
            original_info=packet_durations(spec['source'],spec['video'],spec['tools'],cancelled)
            duration=next(s['duration'] for s in original_info['streams'] if s['index']==spec['video_index'])
            verification_spec=dict(spec,video=original_info,duration=duration)
    verify_output(verification_spec, inspected)
    if remux:
        verify_remux_packets(spec, temp, inspected, cancelled)
    verify_subtitles(spec, temp, inspected, cancelled)
    verify_external(spec, temp, inspected, cancelled)
    if spec.get("container") == "webm":
        verify_webm_header(temp)
    run_process([spec["tools"]["ffmpeg"], "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-nostats",
        "-threads", str(spec["threads"]), "-err_detect", "explode", "-i", str(temp), "-map", "0:v:0", "-map", "0:a?",
        "-fps_mode:v", "passthrough", "-enc_time_base:v", "demux", "-f", "null", "-"], cancelled, on_line=lambda line: None)
    progress(95, 100)
    digest = hash_file(temp, cancelled)
    if hash_file(spec["source"], cancelled) != original_hash:
        raise ValueError("Original changed during conversion. Output was not committed.")
    check_source(spec)
    verified_identity = identity(regular(temp))
    if "budget" in spec and verified_identity[2] > spec["budget"]["target_bytes"]:
        raise ValueError("Target missed: output grew beyond the cap during verification. Original kept.")
    store.set_item(item["id"], "committing", "Verified video; committing output", digest=digest, source_digest=original_hash)
    hook("prepared")
    checkpoint(cancelled)
    check_parent(spec)
    check_source(spec)
    check_external(spec.get("external_subtitle"), cancelled)
    if identity(regular(temp)) != verified_identity:
        raise ValueError("Temporary video changed after verification.")
    if spec.get("reuse_analysis"):
        cleanup_scope(store, item["id"])
    commit_no_replace(temp, output)
    hook("committed")
    progress(100, 100)
    w, h = spec["output_size"]
    audio_label = " + Opus" if spec.get("codec") == "vp9" else " + AAC"
    result = f"{spec.get('container', 'mp4').upper()} / {codec['label']}{audio_label if spec['audio_index'] is not None else ', no audio'}; {w} × {h}; fully decoded. {spec['identity'][2]:,} → {output.stat().st_size:,} bytes. Original SHA-256 unchanged."
    result += f" Kept {len(audio_specs(spec))} audio and {len(spec.get('subtitle_tracks', []))} subtitle track(s)."
    if remux:
        result = (f"{spec['container'].upper()} / {codec['label']}; remuxed video/audio without re-encoding. "
                  f"Packet content/timing verified; fully decoded. {spec['identity'][2]:,} → {output.stat().st_size:,} bytes. "
                  f"Kept {len(spec['audio_tracks'])} audio and {len(spec['subtitle_tracks'])} subtitle track(s). Original SHA-256 unchanged.")
    return (measured_size_message(spec["budget"], output.stat().st_size) + " " if "budget" in spec else "") + result
