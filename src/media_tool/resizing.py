"""Pure, deterministic geometry shared by live preview and execution plans."""
from fractions import Fraction

MAX_PIXELS = 40_000_000
MAX_SIDE = 40000


def validate_resize(mode, width, height, percent):
    if mode not in ("none", "dimensions", "percent"):
        raise ValueError("Choose original size, width and height, or percentage.")
    if mode == "dimensions" and any(type(value) is not int or not 1 <= value <= MAX_SIDE for value in (width, height)):
        raise ValueError("Width and height must be between 1 and 40,000 pixels.")
    if mode == "percent" and (type(percent) is not int or not 1 <= percent <= 400):
        raise ValueError("Scale must be between 1 and 400 percent.")


def checked_size(size):
    if len(size) != 2 or any(type(value) is not int or value < 1 for value in size):
        raise ValueError("Output dimensions must be positive whole pixels.")
    if size[0] * size[1] > MAX_PIXELS:
        raise ValueError("Result exceeds the 40 megapixel limit. Choose smaller dimensions or scale.")
    return tuple(size)


def resize_dimensions(size, mode="none", width=1920, height=1080, percent=50,
                      aspect=True, no_upscale=True):
    validate_resize(mode, width, height, percent)
    w, h = checked_size(size)
    if mode == "none":
        return w, h
    if mode == "dimensions" and not aspect:
        return checked_size((min(width, w), min(height, h)) if no_upscale else (width, height))
    scale = min(Fraction(width, w), Fraction(height, h)) if mode == "dimensions" else Fraction(percent, 100)
    if no_upscale:
        scale = min(scale, Fraction(1))
    # Round half up, minimum one pixel; integer arithmetic avoids float drift.
    result = tuple(max(1, (value * scale.numerator * 2 + scale.denominator) // (2 * scale.denominator))
                   for value in (w, h))
    return checked_size(result)
