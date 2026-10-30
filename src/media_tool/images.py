"""Still-image planning and verified output. Pillow is imported only by workers."""
from dataclasses import replace
from io import BytesIO
import os
from pathlib import Path
import re
import json
import uuid

from .jobs import (identity, regular, check_source, checkpoint, hash_file, owns_temp,
                   cleanup_temp, commit_no_replace)
from .planning import build_plan, OperationPlan, PlanCancelled
from .resizing import resize_dimensions, validate_resize, checked_size

MAX_PIXELS = 40_000_000
FORMATS = {"jpg": "JPEG", "png": "PNG", "webp": "WEBP"}


def inspect_image(image):
    if image.format not in ("JPEG", "PNG", "WEBP", "BMP", "TIFF"):
        raise ValueError("Supported inputs: JPEG, PNG, WebP, BMP and single-page TIFF.")
    if image.width * image.height > MAX_PIXELS:
        raise ValueError("Image exceeds the 40 megapixel limit for this release.")
    if getattr(image, "n_frames", 1) != 1:
        raise ValueError("Animated images and multipage images are not supported yet.")
    if image.mode not in ("1", "L", "LA", "P", "RGB", "RGBA", "CMYK"):
        raise ValueError(f"Unsupported pixel mode {image.mode}; high-bit-depth images are not converted silently.")
    orientation = image.getexif().get(274, 1)
    size = (image.height, image.width) if orientation in (5, 6, 7, 8) else image.size
    return {"format": image.format, "mode": image.mode, "size": list(image.size),
            "output_size": list(size), "icc": bool(image.info.get("icc_profile"))}


def plan_images(records, request, cancelled=lambda: False):
    from PIL import Image
    validate_resize(request.resize_mode, request.resize_width, request.resize_height, request.resize_percent)
    if not 1 <= request.image_quality <= 100:
        raise ValueError("Image quality must be between 1 and 100.")
    if request.metadata not in ("remove", "preserve"):
        raise ValueError("Choose Remove or Preserve supported metadata.")
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", request.alpha_background):
        raise ValueError("JPEG background must be a six-digit color, e.g. #ffffff.")
    plan = build_plan(records, request, cancelled)
    if request.destination.strip() and not Path(request.destination).expanduser().is_dir():
        raise ValueError("Choose an existing output folder before converting images.")
    items = []
    for item in plan.items:
        if cancelled():
            raise PlanCancelled()
        if item.status != "Planned":
            items.append(item)
            continue
        try:
            parent = Path(item.output).parent.resolve(strict=True)
            if not parent.is_dir() or not parent.stat().st_ino:
                raise ValueError("Choose an existing output folder with stable file identities.")
            parent_id = identity(parent.stat())[:2]
            source_id = [item.source.device, item.source.inode, item.source.size, item.source.modified_ns]
            spec = {"source": item.source.path, "identity": source_id,
                    "output": str(parent / Path(item.output).name), "parent": str(parent),
                    "parent_identity": parent_id, "name": item.source.name,
                    "extension": request.extension, "quality": request.image_quality,
                    "background": request.alpha_background, "metadata": request.metadata,
                    "keep_originals": request.keep_originals}
            check_source(spec)
            with Image.open(item.source.path) as image:
                spec["image"] = inspect_image(image)
            # PNG EXIF inspection may load pixels. verify() needs a fresh decoder.
            with Image.open(item.source.path) as image:
                image.verify()
            check_source(spec)
            original_size = spec["image"]["output_size"]
            spec["output_size"] = list(resize_dimensions(original_size, request.resize_mode, request.resize_width,
                request.resize_height, request.resize_percent, request.resize_aspect, request.resize_no_upscale))
            spec["resize"] = dict(mode=request.resize_mode, width=request.resize_width, height=request.resize_height,
                                  percent=request.resize_percent, aspect=request.resize_aspect, no_upscale=request.resize_no_upscale)
            w, h = spec["output_size"]
            reason = f"{spec['image']['format']} → {FORMATS[request.extension]}; {original_size[0]} × {original_size[1]} → {w} × {h}; orientation normalized."
            if request.resize_mode == "dimensions" and not request.resize_aspect:
                reason += " Aspect ratio unlocked."
            if spec["image"]["mode"] == "CMYK" and not spec["image"]["icc"]:
                reason += " Unprofiled CMYK uses an approximate RGB conversion."
            if not request.keep_originals:
                reason += " Original will move to the Recycle Bin only after this output is verified and saved."
            items.append(replace(item, output=spec["output"], reason=reason, image_spec=spec))
        except Exception as error:
            items.append(replace(item, status="Invalid", reason=f"Cannot convert: {error}", image_spec=None))
    return OperationPlan(request, tuple(items), (
        "Originals are kept. Preview dimensions include orientation correction." if request.keep_originals else
        "Verified originals will move to the Recycle Bin after successful conversion. Failed/cancelled files are kept.",
        "ICC colors convert to sRGB. Preserve keeps supported EXIF, sRGB ICC and DPI; other metadata is omitted.",
    ))


def image_snapshot(plan):
    if plan.request.operation != "images" or any(i.status not in ("Planned", "Skipped") for i in plan.items):
        raise ValueError("Remove invalid or unsupported inputs, then preview again. No files changed.")
    if not any(i.status == "Planned" for i in plan.items):
        raise ValueError("There are no images to convert.")
    specs = []
    for item in plan.items:
        if item.status == "Planned":
            if item.image_spec is None:
                raise ValueError("Inspect images before queuing conversion.")
            specs.append(item.image_spec)
        else:
            specs.append({"source": item.source.path, "output": item.output,
                          "identity": [item.source.device, item.source.inode, item.source.size, item.source.modified_ns],
                          "initial_state": "skipped", "initial_message": item.reason})
    return tuple(specs)


def normalized_pixels(source):
    """Return oriented RGB(A) in sRGB when an embedded profile is available."""
    from PIL import Image, ImageOps, ImageCms
    image = ImageOps.exif_transpose(source)
    alpha = image.getchannel("A") if "A" in image.getbands() else (
        image.convert("RGBA").getchannel("A") if "transparency" in image.info else None)
    profile = image.info.get("icc_profile")
    srgb = None
    if profile:
        target = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
        original = ImageCms.ImageCmsProfile(BytesIO(profile))
        # Preserve L/CMYK channels when the embedded profile describes those modes.
        color = image.getchannel("L") if image.mode == "LA" else (
            image if image.mode in ("RGB", "L", "CMYK") else image.convert("RGB"))
        converted = ImageCms.profileToProfile(color, original, target, outputMode="RGB")
        srgb = target.tobytes()
    else:
        converted = image.convert("RGB")
    if alpha is not None:
        converted.putalpha(alpha)
    return image, converted, srgb


def encode(source, destination, spec):
    from PIL import Image, ImageColor
    oriented, pixels, profile = normalized_pixels(source)
    try:
        exif = oriented.getexif()
        oriented.close()
        target = checked_size(spec.get("output_size", spec["image"]["output_size"]))
        if pixels.size != target:
            resized = pixels.resize(target, Image.Resampling.LANCZOS)
            pixels.close()
            pixels = resized
        options = {}
        if spec["metadata"] == "preserve":
            exif.pop(274, None)
            for tag, value in ((256, pixels.width), (257, pixels.height)):
                if tag in exif:
                    exif[tag] = value
            if 34665 in exif:
                nested = exif.get_ifd(34665)
                for tag, value in ((40962, pixels.width), (40963, pixels.height)):
                    if tag in nested:
                        nested[tag] = value
            if exif:
                options["exif"] = exif.tobytes()
            if profile:
                options["icc_profile"] = profile
            if "dpi" in source.info and spec["extension"] != "webp":
                options["dpi"] = source.info["dpi"]
        if spec["extension"] == "jpg" and pixels.mode == "RGBA":
            flattened = Image.new("RGB", pixels.size, ImageColor.getrgb(spec["background"]))
            flattened.paste(pixels, mask=pixels.getchannel("A"))
            pixels.close()
            pixels = flattened
        # Clear inherited info so Remove cannot accidentally retain EXIF or ICC.
        pixels.info.clear()
        if spec["extension"] in ("jpg", "webp"):
            options["quality"] = spec["quality"]
        if spec["extension"] == "jpg":
            options["subsampling"] = 0
        pixels.save(destination, format=FORMATS[spec["extension"]], **options)
    finally:
        pixels.close()
        oriented.close()


def check_parent(spec):
    parent = Path(spec["parent"])
    if (Path(spec["output"]).parent != parent or parent.resolve(strict=True) != parent
            or identity(parent.stat())[:2] != spec["parent_identity"]):
        raise ValueError("Output folder changed. Preview a new conversion.")
    return parent


def run_image(store, item, cancelled, progress, hook=lambda point: None):
    from PIL import Image
    spec = json.loads(item["spec"])
    target_size = checked_size(spec.get("output_size", spec["image"]["output_size"]))
    parent = check_parent(spec)
    output = Path(spec["output"])
    if item["digest"] and owns_temp(item, output):
        if hash_file(output, cancelled) != item["digest"]:
            raise ValueError("Previously committed image changed; it was left untouched.")
        cleanup_temp(store, item["id"])
        return "Recovered verified converted image. Original kept."
    if item["temp"] and os.path.lexists(item["temp"]) and not cleanup_temp(store, item["id"]):
        raise ValueError("Temporary path belongs to another file; it was left untouched.")
    checkpoint(cancelled)
    check_source(spec)
    if os.path.lexists(output):
        raise FileExistsError("Output appeared after preview; existing file was kept.")
    original_hash = hash_file(spec["source"], cancelled)
    progress(10, 100)
    temp = parent / f".mediatool-{item['job_id']}-{item['id']}-{uuid.uuid4().hex}.part"
    with temp.open("xb") as destination:
        info = os.fstat(destination.fileno())
        store.set_item(item["id"], "running", "Decoding and converting image", temp=str(temp),
                       temp_dev=str(info.st_dev), temp_ino=str(info.st_ino), digest=None)
        hook("created")
        checkpoint(cancelled)
        with Path(spec["source"]).open("rb") as handle:
            check_source(spec, handle)
            with Image.open(handle) as source:
                if inspect_image(source) != spec["image"]:
                    raise ValueError("Image details changed after preview.")
                source.load()
                checkpoint(cancelled)
                encode(source, destination, spec)
            check_source(spec, handle)
        destination.flush()
        os.fsync(destination.fileno())
    hook("written")
    checkpoint(cancelled)
    progress(75, 100)
    store.set_item(item["id"], "verifying", "Decoding output and checking original SHA-256")
    with Image.open(temp) as verified:
        verified.load()
        if verified.format != FORMATS[spec["extension"]] or verified.size != target_size:
            raise ValueError("Converted image failed format or dimension verification.")
    digest = hash_file(temp, cancelled)
    if hash_file(spec["source"], cancelled) != original_hash:
        raise ValueError("Original changed during conversion. Output was not committed.")
    check_source(spec)
    verified_identity = identity(regular(temp))
    store.set_item(item["id"], "committing", "Verified image; committing output", digest=digest, source_digest=original_hash)
    hook("prepared")
    checkpoint(cancelled)
    check_parent(spec)
    check_source(spec)
    if identity(regular(temp)) != verified_identity:
        raise ValueError("Temporary image changed after verification.")
    commit_no_replace(temp, output)
    hook("committed")
    progress(100, 100)
    w, h = target_size
    iw, ih = spec["image"]["output_size"]
    return (f"{spec['image']['format']} → {FORMATS[spec['extension']]}; {iw} × {ih} → {w} × {h}; "
            f"{spec['identity'][2]:,} → {output.stat().st_size:,} bytes. Original SHA-256 unchanged.")
