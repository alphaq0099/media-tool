"""Packaged smoke checks with development-runtime paths removed."""
from pathlib import Path
import argparse
import json
import os
import subprocess
import shutil
import sys
from PIL import Image

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("--exe", type=Path, default=root / "dist" / "MediaTool" / ("MediaTool.exe" if os.name == "nt" else "MediaTool"))
parser.add_argument("--tag", default="")
parser.add_argument("--queue-check", action="store_true", help="Also verify phase-2 packaged jobs with disposable fixtures")
parser.add_argument("--rename-check", action="store_true", help="Also verify packaged rename jobs with disposable fixtures")
parser.add_argument("--undo-check", action="store_true", help="Also verify packaged rename undo with disposable fixtures")
parser.add_argument("--image-check", action="store_true", help="Also verify packaged still-image conversion")
parser.add_argument("--resize-check", action="store_true", help="Also verify packaged image resizing")
parser.add_argument("--video-check", action="store_true", help="Also verify packaged video conversion with external FFmpeg")
parser.add_argument("--compress-check", action="store_true", help="Also verify packaged target-size compression")
parser.add_argument("--format-check", action="store_true", help="Also verify packaged H.265/MKV conversion")
parser.add_argument("--webm-check", action="store_true", help="Also verify packaged VP9/Opus WebM conversion")
parser.add_argument("--tracks-check", action="store_true", help="Also verify packaged audio/subtitle selection")
parser.add_argument("--remux-check", action="store_true", help="Also verify packaged video/audio stream copy")
parser.add_argument("--hevc-size-check", action="store_true", help="Also verify packaged H.265 two-pass compression")
parser.add_argument("--archive-check", action="store_true", help="Also verify packaged ZIP creation and contents preview")
options = parser.parse_args()
exe = options.exe.resolve()
tag = options.tag + "-" if options.tag else ""
artifacts = root / "artifacts"
artifacts.mkdir(exist_ok=True)

def fresh_settings(name):
    path=artifacts / f"{tag}{name}-package.ini"
    path.unlink(missing_ok=True)
    return str(path)
environment = os.environ.copy()
for key in ("PYTHONPATH", "PYTHONHOME", "QT_PLUGIN_PATH", "QML2_IMPORT_PATH", "QT_QPA_PLATFORM"):
    environment.pop(key, None)
if os.name == "nt":
    environment["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
results = []
for theme, scale in (("light", "1"), ("dark", "1"), ("dark", "1.5"), ("dark", "2")):
    target = artifacts / f"{tag}packaged-{theme}-{scale}.png"
    if target.exists():
        target.unlink()
    environment["QT_SCALE_FACTOR"] = scale
    result = subprocess.run([
        str(exe), "--screenshot", str(target), "--sample-rows", "3",
        "--settings-file", fresh_settings("capture"), "--theme", theme,
    ], cwd=artifacts, env=environment, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode != 0 or not target.exists():
        raise RuntimeError(f"Package smoke failed: {theme}, scale {scale}")
    with Image.open(target) as image:
        dimensions = image.size
    results.append(dict(theme=theme, scale=scale, screenshot=target.name, pixels=dimensions, exit_code=result.returncode))
    print(f"PASS: packaged {theme}, scale {scale}, {dimensions}", flush=True)
queue_result = None
if options.queue_check:
    target = artifacts / f"{tag}packaged-queue-check.json"
    target.unlink(missing_ok=True)
    environment["QT_SCALE_FACTOR"] = "1"
    result = subprocess.run([
        str(exe), "--queue-check", str(target),
        "--settings-file", fresh_settings("queue"),
        "--exit-after-ms", "30000",
    ], cwd=artifacts, env=environment, timeout=40,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode != 0 or not target.exists():
        raise RuntimeError("Packaged queue diagnostic did not finish")
    queue_result = json.loads(target.read_text(encoding="utf-8"))
    if not queue_result.get("passed"):
        raise RuntimeError(f"Packaged queue checks failed: {queue_result}")
    print("PASS: packaged queue, verified copies, cancellation, retry, history reopen, concurrent intake", flush=True)
rename_result = None
if options.rename_check:
    target = artifacts / f"{tag}packaged-rename-check.json"
    target.unlink(missing_ok=True)
    environment["QT_SCALE_FACTOR"] = "1"
    result = subprocess.run([
        str(exe), "--rename-check", str(target),
        "--settings-file", fresh_settings("rename"),
        "--exit-after-ms", "40000",
    ], cwd=artifacts, env=environment, timeout=45,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode != 0 or not target.exists():
        raise RuntimeError("Packaged rename diagnostic did not finish")
    rename_result = json.loads(target.read_text(encoding="utf-8"))
    if not rename_result.get("passed"):
        raise RuntimeError(f"Packaged rename checks failed: {rename_result}")
    print("PASS: packaged direct rename, 20 files, 2 collisions, preserved contents, workspace results, waiting queue untouched", flush=True)
undo_result = None
if options.undo_check:
    target = artifacts / f"{tag}packaged-undo-check.json"
    target.unlink(missing_ok=True)
    environment["QT_SCALE_FACTOR"] = "1"
    result = subprocess.run([
        str(exe), "--undo-check", str(target),
        "--settings-file", fresh_settings("undo"),
        "--exit-after-ms", "70000",
    ], cwd=artifacts, env=environment, timeout=75,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode != 0 or not target.exists():
        raise RuntimeError("Packaged undo diagnostic did not finish")
    undo_result = json.loads(target.read_text(encoding="utf-8"))
    if not undo_result.get("passed"):
        raise RuntimeError(f"Packaged undo checks failed: {undo_result}")
    print("PASS: packaged undo, exact names restored, conflicts blocked, history reopened", flush=True)
image_result = None
if options.image_check:
    target = artifacts / f"{tag}packaged-image-check.json"
    target.unlink(missing_ok=True)
    environment["QT_SCALE_FACTOR"] = "1"
    result = subprocess.run([
        str(exe), "--image-check", str(target),
        "--settings-file", fresh_settings("image"),
        "--exit-after-ms", "65000",
    ], cwd=artifacts, env=environment, timeout=70,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode != 0 or not target.exists():
        raise RuntimeError("Packaged image diagnostic did not finish")
    image_result = json.loads(target.read_text(encoding="utf-8"))
    if not image_result.get("passed"):
        raise RuntimeError(f"Packaged image checks failed: {image_result}")
    print("PASS: packaged image conversion, original hashes, output decode and comparison", flush=True)
resize_result = None
if options.resize_check:
    target = artifacts / f"{tag}packaged-resize-check.json"
    target.unlink(missing_ok=True)
    environment["QT_SCALE_FACTOR"] = "1"
    result = subprocess.run([
        str(exe), "--resize-check", str(target),
        "--settings-file", fresh_settings("resize"),
        "--exit-after-ms", "65000",
    ], cwd=artifacts, env=environment, timeout=70,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode != 0 or not target.exists():
        raise RuntimeError("Packaged resize diagnostic did not finish")
    resize_result = json.loads(target.read_text(encoding="utf-8"))
    if not resize_result.get("passed"):
        raise RuntimeError(f"Packaged resize checks failed: {resize_result}")
    print("PASS: packaged resizing, output dimensions, original hashes and comparison", flush=True)
video_result = compression_result = format_result = webm_result = tracks_result = remux_result = hevc_size_result = archive_result = None
for enabled, check_name in ((options.video_check, "video"), (options.compress_check, "compress"), (options.format_check, "format"), (options.webm_check, "webm"), (options.tracks_check, "tracks"), (options.remux_check, "remux"), (options.hevc_size_check, "hevc-size"), (options.archive_check, "archive")):
    if not enabled:
        continue
    target = artifacts / f"{tag}packaged-{check_name}-check.json"
    target.unlink(missing_ok=True)
    video_environment = environment.copy()
    video_environment["QT_SCALE_FACTOR"] = "1"
    # FFmpeg is an intentional external dependency; keep Python/venv paths excluded.
    folders = []
    for name in ("ffmpeg", "ffprobe"):
        tool = shutil.which(name)
        if tool:
            folders.append(str(Path(tool).resolve().parent))
    video_environment["PATH"] = os.pathsep.join(dict.fromkeys([*folders, environment.get("PATH", "")]))
    result = subprocess.run([
        str(exe), f"--{check_name}-check", str(target),
        "--settings-file", fresh_settings(check_name),
        "--exit-after-ms", "110000",
    ], cwd=artifacts, env=video_environment, timeout=115,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode != 0 or not target.exists():
        raise RuntimeError(f"Packaged {check_name} diagnostic did not finish")
    check_result = json.loads(target.read_text(encoding="utf-8"))
    if not check_result.get("passed"):
        raise RuntimeError(f"Packaged {check_name} checks failed: {check_result}")
    if check_name == "video":
        video_result = check_result
    elif check_name == "compress":
        compression_result = check_result
    elif check_name == "format":
        format_result = check_result
    elif check_name == "webm":
        webm_result = check_result
    elif check_name == "tracks":
        tracks_result = check_result
    elif check_name == "remux":
        remux_result = check_result
    elif check_name == "hevc-size":
        hevc_size_result = check_result
    else:
        archive_result = check_result
    print(f"PASS: packaged {check_name}, stream review, original hashes, dimensions, output decode and applicable byte caps", flush=True)
(artifacts / (tag + "package-smoke.json")).write_text(json.dumps({
    "mode": "Native Qt platform, off-screen screenshots, sanitized Windows PATH.",
    "limitations": "Does not replace a clean VM, native-dialog interaction, real display scaling, or assistive-technology testing.",
    "cases": results,
    "queue_check": queue_result,
    "rename_check": rename_result,
    "image_check": image_result,
    "resize_check": resize_result,
    "video_check": video_result,
    "compression_check": compression_result,
    "format_check": format_result,
    "webm_check": webm_result,
    "tracks_check": tracks_result,
    "remux_check": remux_result,
    "hevc_size_check": hevc_size_result,
    "archive_check": archive_result,
    "undo_check": undo_result,
}, indent=2), encoding="utf-8")
