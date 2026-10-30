"""Explicit per-file byte caps and conservative two-pass video budgets."""
from decimal import Decimal, InvalidOperation
import math

UNITS = {"MB": 1_000_000, "MiB": 1_048_576}
MIN_VIDEO_RATE = 32_000


def target_bytes(amount, unit):
    try:
        value = Decimal(str(amount))
        if not value.is_finite() or not Decimal("0.01") <= value <= 1_000_000 or unit not in UNITS:
            raise ValueError()
        return int(value * UNITS[unit])
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("Target size must be 0.01–1,000,000 MB or MiB per file.") from error


def size_budget(amount, unit, audio_kbps, info, video, audio):
    cap = target_bytes(amount, unit)
    if audio_kbps not in (64, 96, 128, 192, 256, 320):
        raise ValueError("Choose a supported audio bitrate.")
    durations = [info.get("duration")]
    audios = audio if isinstance(audio, list) else ([audio] if audio is not None else [])
    for selected in [video, *audios]:
        if selected is not None:
            duration = selected.get("duration")
            if duration is None and info.get("duration") is None:
                raise ValueError("Target size needs a known duration for the selected video/audio. Use Quality mode.")
            if duration is not None:
                durations.append(duration + max(0, selected.get("start") or 0))
    known = [value for value in durations if value is not None and math.isfinite(value) and value > 0]
    if not known:
        raise ValueError("Target size needs a known duration. Use Quality mode.")
    duration = max(known)
    overhead = max(16_384, math.ceil(cap * 0.03))
    audio_rate = audio_kbps * 1000 if audios else 0
    # AAC uses an average bitrate: allow variation as well as MP4 overhead.
    audio_bytes = math.ceil(audio_rate * len(audios) * duration / 8 * 1.05)
    available = cap - overhead - audio_bytes
    video_rate = min(100_000_000, math.floor(available * 8 / duration))
    if video_rate < MIN_VIDEO_RATE:
        raise ValueError("Target is too small for duration, audio and container overhead. Increase size, lower audio bitrate or choose No audio.")
    return dict(target_bytes=cap, amount=str(amount), unit=unit, duration=duration,
                overhead_bytes=overhead, audio_bytes=audio_bytes, audio_bitrate=audio_rate,
                audio_count=len(audios), total_audio_bitrate=audio_rate * len(audios),
                video_bitrate=video_rate, container_duration_used=video.get("duration") is None or
                any(track.get("duration") is None for track in audios))


def budget_description(budget, size, frame_rate):
    text = (f"Maximum {budget['amount']} {budget['unit']} per file ({budget['target_bytes']:,} bytes). "
            f"Two-pass video {budget['video_bitrate'] / 1000:,.0f} kb/s; "
            f"audio {budget['audio_bitrate'] // 1000} kb/s per track × {budget.get('audio_count', int(bool(budget['audio_bitrate'])))}; "
            f"container reserve {budget['overhead_bytes']:,} bytes. Final size checked.")
    if budget["container_duration_used"]:
        text += " Budget uses container duration; final timing and byte cap are verified."
    try:
        from fractions import Fraction
        fps = float(Fraction(frame_rate))
        if fps > 0 and budget["video_bitrate"] / (size[0] * size[1] * fps) < 0.06:
            text += " Tight budget for this resolution: consider resizing or a larger target. This is a heuristic, not a quality prediction."
    except (ValueError, TypeError, ZeroDivisionError):
        pass
    return text


def lower_video_rate(budget, measured, current):
    factor = min(0.85, budget["target_bytes"] / max(1, measured) * 0.90)
    return max(MIN_VIDEO_RATE, int(current * factor))


def measured_size_message(budget, measured):
    return (f"Target met: {measured / UNITS[budget['unit']]:.3f} / {budget['amount']} {budget['unit']} "
            f"({measured:,} / {budget['target_bytes']:,} bytes).")
