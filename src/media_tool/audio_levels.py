"""Measured volume and two-pass loudness, with post-encode checks."""
from dataclasses import replace
import json
import math
from pathlib import Path
import re

from .audio_edit import reserve_output
from .domain import path_key
from .jobs import check_source
from .planning import OperationPlan, PlanCancelled
from .processes import run_process


def finite(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def measure(spec, path, cancelled=lambda: False, source=False, target=-16, peak=-1.5):
    settings = spec["settings"]
    args = [spec["tools"]["ffmpeg"], "-hide_banner", "-nostdin", "-v", "info", "-nostats", "-threads", "2",
        "-protocol_whitelist", "file,pipe", "-i", str(path), "-map", f"0:{spec['stream']['index']}" if source else "0:a:0",
        "-vn", "-sn", "-dn", "-af", f"aformat=channel_layouts={settings['channel_layout']},aresample={settings['sample_rate']},loudnorm=I={target}:TP={peak}:LRA=11:print_format=json", "-f", "null", "-"]
    text = run_process(args, cancelled, include_stderr=True)
    matches = re.findall(r'\{\s*"input_i".*?\}', text, re.S)
    if not matches:
        raise ValueError("FFmpeg could not measure audio loudness.")
    data = json.loads(matches[-1])
    return {key: str(data[key]) for key in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")}


def plan_levels(records, request, cancelled=lambda: False):
    from .audio import plan_conversion
    if request.audio_mode != "encode":
        raise ValueError("Volume and loudness changes require Convert audio.")
    if not math.isfinite(request.audio_gain) or not -60 <= request.audio_gain <= 24:
        raise ValueError("Volume gain must be between -60 and +24 dB.")
    if not math.isfinite(request.audio_lufs) or not -70 <= request.audio_lufs <= -5:
        raise ValueError("Loudness target must be between -70 and -5 LUFS.")
    if not math.isfinite(request.audio_true_peak) or not -9 <= request.audio_true_peak <= 0:
        raise ValueError("True-peak limit must be between -9 and 0 dBTP.")
    base = plan_conversion(records, replace(request, audio_action="convert", collision="unique"), cancelled)
    reserved = {path_key(r.path) for r in records if not r.sample}
    items = []
    for item in base.items:
        if item.status != "Planned":
            items.append(item)
            continue
        try:
            spec = dict(item.audio_spec)
            data = measure(spec, spec["source"], cancelled, source=True, target=request.audio_lufs, peak=max(-9, request.audio_true_peak-.5))
            check_source(spec)
            loudness, peak = finite(data["input_i"]), finite(data["input_tp"])
            if request.audio_action == "normalize" and (loudness is None or loudness < -69 or any(finite(v) is None for v in data.values())):
                raise ValueError("This track is silent, too quiet or too short for reliable loudness normalization.")
            if request.audio_action == "volume" and peak is not None and peak + request.audio_gain > -.05:
                raise ValueError("This gain would clip the signal. Reduce volume gain or use loudness normalization.")
            spec["levels"] = dict(action=request.audio_action, input=data, gain=request.audio_gain,
                                   target=request.audio_lufs, peak=request.audio_true_peak)
            suffix = "normalized" if request.audio_action == "normalize" else "volume"
            track = f"_track{spec['track']:02d}" if len(spec["input_info"]["streams"]) > 1 else ""
            output, status = reserve_output(Path(spec["parent"]), f"{Path(item.source.name).stem}{track}_{suffix}.{request.extension}", request.collision, reserved, cancelled)
            spec["output"] = output
            before = f"{loudness:.2f} LUFS" if loudness is not None else "unmeasurable/silent"
            reason = f"{before}; peak {peak:.2f} dBTP" if peak is not None else before
            reason += f" → {request.audio_lufs:g} LUFS; peak ≤ {request.audio_true_peak:g} dBTP" if request.audio_action == "normalize" else f"; gain {request.audio_gain:+g} dB"
            items.append(replace(item, output=output, status=status, reason=reason, audio_spec=spec))
        except Exception as error:
            if cancelled():
                raise PlanCancelled() from error
            items.append(replace(item, status="Invalid", reason=str(error), audio_spec=None))
    return OperationPlan(request, tuple(items))


def level_command(spec, temp):
    from .audio import encode_command
    plain = dict(spec)
    levels = plain.pop("levels")
    args = encode_command(plain, temp)
    index = args.index("-af") + 1
    if levels["action"] == "volume":
        filter_text = f"volume={levels['gain']:.6f}dB"
    else:
        data = levels["input"]
        # Encode with headroom for lossy codec peak overshoot; reopen and measure
        # the actual output before committing, never just trust filter estimates.
        peak = max(-9, levels["peak"] - .5)
        filter_text = (f"loudnorm=I={levels['target']}:TP={peak}:LRA=11:linear=true:"
            f"measured_I={data['input_i']}:measured_TP={data['input_tp']}:measured_LRA={data['input_lra']}:"
            f"measured_thresh={data['input_thresh']}:offset={data['target_offset']}")
    settings = spec["settings"]
    args[index] += f",aformat=channel_layouts={settings['channel_layout']},aresample={settings['sample_rate']}," + filter_text
    return args


def verify_levels(spec, path, cancelled):
    levels = spec["levels"]
    measured = measure(spec, path, cancelled)
    loudness, peak = finite(measured["input_i"]), finite(measured["input_tp"])
    if levels["action"] == "normalize":
        if loudness is None or peak is None or abs(loudness-levels["target"]) > .5 or peak > levels["peak"] + .15:
            raise ValueError("Output did not meet loudness (±0.5 LU) or true-peak limits. Choose a lower loudness target; no final output was saved.")
    elif peak is not None and peak > .05:
        raise ValueError("Encoded output clips. Lower volume gain; no final output was saved.")
    display_i = f"{loudness:.2f} LUFS" if loudness is not None else "silent/unmeasurable loudness"
    display_p = f"{peak:.2f} dBTP" if peak is not None else "no measurable peak"
    return f" Measured output: {display_i}; {display_p}."
