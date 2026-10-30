"""Display-aware video geometry, shared by planning and live preview."""
from fractions import Fraction
import re

from .resizing import MAX_PIXELS, MAX_SIDE, validate_resize


def display_dimensions(video):
    w, h = video["width"], video["height"]
    if any(type(value) is not int or value < 2 for value in (w, h)) or w * h > MAX_PIXELS:
        raise ValueError("Unsupported video dimensions; the input limit is 40 MP.")
    try:
        raw = video.get("sample_aspect_ratio")
        sar = Fraction(str(raw).replace(":", "/")) if raw not in (None, "N/A", "0:1") else Fraction(1)
        if sar <= 0:
            raise ValueError()
    except (ValueError, ZeroDivisionError) as error:
        raise ValueError("Invalid video pixel aspect ratio.") from error
    rotation = video.get("rotation", 0)
    if rotation is None or abs(rotation - round(rotation / 90) * 90) > 0.01:
        raise ValueError("Only 90°, 180° and 270° rotation metadata is supported.")
    # Arbitrary display transforms need more than an angle and must not be guessed.
    matrix = video.get("display_matrix")
    if matrix:
        values = [int(value) for row in matrix.splitlines() if ":" in row
                  for value in re.findall(r"-?\d+", row.partition(":")[2])]
        if (len(values) != 9 or any(values[i] for i in (2, 5, 6, 7)) or values[8] != 1 << 30
                or any(value not in (-65536, 0, 65536) for value in (values[0], values[1], values[3], values[4]))
                or values[0] ** 2 + values[1] ** 2 != 65536 ** 2
                or values[3] ** 2 + values[4] ** 2 != 65536 ** 2
                or values[0] * values[4] - values[1] * values[3] != 65536 ** 2):
            raise ValueError("Mirrored, scaled or arbitrary display matrices are not supported in this phase.")
    size = (Fraction(w) * sar, Fraction(h))
    if round(rotation / 90) % 2:
        size = size[::-1]
    if min(size) < 2 or max(size) > MAX_SIDE or size[0] * size[1] > MAX_PIXELS:
        raise ValueError("Normalized display exceeds the supported 40 MP / 40,000-pixel limits.")
    return size


def video_dimensions(video, mode="none", width=1920, height=1080, percent=50,
                     aspect=True, no_upscale=True):
    validate_resize(mode, width, height, percent)
    if mode != "none" and not aspect:
        raise ValueError("Video resizing keeps the aspect ratio. Enable Keep aspect ratio.")
    size = display_dimensions(video)
    scale = (min(Fraction(width) / size[0], Fraction(height) / size[1]) if mode == "dimensions"
             else Fraction(percent, 100) if mode == "percent" else Fraction(1))
    if no_upscale:
        scale = min(scale, Fraction(1))
    # Round down to even pixels for H.264/yuv420p, never beyond the requested box.
    output = tuple(int(side * scale // 2) * 2 for side in size)
    if min(output) < 2:
        raise ValueError("Resize would produce less than 2 pixels on one side. Choose larger dimensions or scale.")
    if max(output) > MAX_SIDE or output[0] * output[1] > MAX_PIXELS:
        raise ValueError("Resized video exceeds the 40 MP / 40,000-pixel limits.")
    return output


def display_label_size(video):
    return tuple(int(side + Fraction(1, 2)) for side in display_dimensions(video))
