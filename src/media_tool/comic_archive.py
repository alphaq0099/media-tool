"""Image-only CBZ manifests; original page bytes and names on disk are untouched."""
from pathlib import Path, PurePosixPath

from .domain import natural_key
from .planning import PlanCancelled


def comic_manifest(manifest, numbered, cancelled):
    from PIL import Image
    from .images import MAX_PIXELS
    pages = sorted((dict(e) for e in manifest["entries"] if not e["folder"]),
                   key=lambda e: (tuple(natural_key(p) for p in PurePosixPath(e["name"]).parts), e["name"]))
    width = max(4, len(str(len(pages))))
    for number, page in enumerate(pages, 1):
        if cancelled():
            raise PlanCancelled()
        with Image.open(page["source"]) as image:
            if image.format not in ("JPEG", "PNG", "WEBP", "GIF", "BMP") or getattr(image, "n_frames", 1) != 1:
                raise ValueError(f"Unsupported comic page (requires a single still image): {page['source']}")
            if image.width * image.height > MAX_PIXELS:
                raise ValueError(f"Comic page exceeds the 40 megapixel limit: {page['source']}")
            image.verify()
        with Image.open(page["source"]) as image:
            image.load()
        if numbered:
            page["name"] = f"{number:0{width}d}{Path(page['name']).suffix.lower()}"
    return dict(entries=pages, directories=manifest["directories"], excluded=manifest["excluded"])
