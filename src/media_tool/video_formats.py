"""Validated video output choices, shared by planning and execution."""
CODECS = {
    "h264": {"label": "H.264", "encoder": "libx264"},
    "hevc": {"label": "H.265 (HEVC)", "encoder": "libx265"},
    "vp9": {"label": "VP9", "encoder": "libvpx-vp9"},
}
CONTAINERS = {"mp4": "mp4", "mkv": "matroska", "webm": "webm"}
COMPATIBLE = {"mp4": ("h264", "hevc"), "mkv": ("h264", "hevc"), "webm": ("vp9",)}
HEVC_QUALITY = {28: 32, 23: 28, 20: 24, 18: 20}
VP9_QUALITY = {28: 40, 23: 32, 20: 26, 18: 20}
VP9_SPEED = {"medium": 2, "veryfast": 4, "superfast": 5}


def output_format(codec="h264", container="mp4"):
    if codec not in COMPATIBLE.get(container, ()):
        raise ValueError("Choose H.264 or H.265 with an MP4 or MKV container, or VP9 with WebM.")
    return CODECS[codec], CONTAINERS[container]


def validate_mode(codec, target):
    if target and codec not in ("h264", "hevc"):
        raise ValueError("Target file size supports H.264 and H.265. Use Quality for VP9.")


def opus_settings(audio):
    layouts = {"mono": 1, "stereo": 2, "3.0": 3, "quad": 4, "5.0": 5, "5.1": 6}
    if layouts.get(audio.get("channel_layout")) != audio["channels"]:
        raise ValueError("WebM Opus needs a known mono, stereo, 3.0, quad, 5.0 or 5.1 audio layout. "
                         "Choose MP4/MKV or No audio for this track; channels are not silently downmixed.")
    return dict(codec="opus", bitrate=128_000 if audio["channels"] <= 2 else 256_000,
                sample_rate=48000, channel_layout=audio["channel_layout"],
                mapping_family=0 if audio["channels"] <= 2 else 1)


def verify_webm_header(path):
    """FFprobe shares a Matroska/WebM name; also check the bounded EBML DocType."""
    with path.open("rb") as handle:
        data = handle.read(4096)

    def integer(offset, identifier=False):
        if offset >= len(data) or not data[offset]:
            raise ValueError("Invalid WebM EBML header.")
        width = 9 - data[offset].bit_length()
        if width > (4 if identifier else 8) or offset + width > len(data):
            raise ValueError("Invalid WebM EBML header.")
        value = int.from_bytes(data[offset:offset + width], "big")
        return (value if identifier else value & ((1 << (7 * width)) - 1)), offset + width

    if data[:4] != b"\x1a\x45\xdf\xa3":
        raise ValueError("Output container is not WebM (missing EBML header).")
    size, offset = integer(4)
    end = offset + size
    if end > len(data):
        raise ValueError("WebM EBML header is truncated or exceeds the inspection limit.")
    types = []
    while offset < end:
        element, offset = integer(offset, True)
        size, offset = integer(offset)
        if offset + size > end:
            raise ValueError("Invalid WebM EBML element size.")
        if element == 0x4282:
            types.append(data[offset:offset + size])
        offset += size
    if types != [b"webm"]:
        raise ValueError("Output container DocType is not WebM.")
