"""Measure the packaged Widgets shell without showing or activating windows."""
import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess
import time
import zipfile
import psutil

parser = argparse.ArgumentParser()
parser.add_argument("--exe", type=Path, required=True)
parser.add_argument("--runs", type=int, default=5)
parser.add_argument("--zip-path", type=Path)
parser.add_argument("--output", type=Path, default=Path("artifacts/benchmark.json"))
options = parser.parse_args()
exe = options.exe.resolve()
root = exe.parent
options.output.parent.mkdir(parents=True, exist_ok=True)
# Read the distribution inventory before launch; this is explicitly a warm-cache
# experiment, never a cold-start benchmark.
inventory = sorted(((str(p.relative_to(root)), p.stat().st_size)
                    for p in root.rglob("*") if p.is_file()), key=lambda item: item[1], reverse=True)
results = []
environment = os.environ.copy()
for key in ('PYTHONPATH', 'PYTHONHOME', 'QT_PLUGIN_PATH', 'QML2_IMPORT_PATH', 'QT_QPA_PLATFORM'):
    environment.pop(key, None)
if os.name == 'nt':
    environment['PATH'] = str(Path(os.environ['SystemRoot']) / 'System32')
for iteration in range(options.runs + 1):
    marker = options.output.parent.resolve() / f"ready-{iteration}.json"
    settings = options.output.parent.resolve() / "benchmark-settings.ini"
    if marker.exists():
        marker.unlink()
    command = [str(exe), "--benchmark-ready", str(marker),
               "--settings-file", str(settings), "--exit-after-ms", "20000"]
    start = time.perf_counter()
    child = subprocess.Popen(command, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=environment)
    try:
        while not marker.exists():
            if child.poll() is not None:
                raise RuntimeError(f"App exited before readiness: {child.returncode}")
            if time.perf_counter() - start > 30:
                raise TimeoutError("Packaged app did not become ready")
            time.sleep(0.005)
        # A file may be visible before the write closes. Retry the tiny JSON read.
        for _ in range(100):
            try:
                ready = json.loads(marker.read_text(encoding="utf-8"))
                break
            except (json.JSONDecodeError, PermissionError):
                time.sleep(0.005)
        else:
            raise RuntimeError("Incomplete ready marker")
        elapsed = ready["ready_perf_counter"] - start
        process = psutil.Process(ready['pid'])
        time.sleep(1)
        memory = process.memory_info()
        cpu = process.cpu_percent(interval=1)
        result = dict(launch_seconds=round(elapsed, 4),
                      working_set_MiB=round(memory.rss/2**20, 2),
                      private_bytes_MiB=round(getattr(memory, "private", memory.vms)/2**20, 2),
                      cpu_percent_one_core_scale=cpu,
                      actual_gui_pid=ready['pid'],
                      child_processes=[p.name() for p in process.children(recursive=True)],
                      loaded_optional_libraries=ready.get('loaded_optional_libraries', []),
                      platform=ready["qt_platform"])
        if iteration:
            results.append(result)
        print(f"{'warmup' if not iteration else iteration}: {result}", flush=True)
        # Let the normal lifecycle close; shorten the run by using a safe WM_CLOSE.
        if os.name == "nt":
            import ctypes
            @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
            def close_window(hwnd, unused):
                pid = ctypes.c_ulong()
                ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value == ready['pid']:
                    ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)
                return True
            ctypes.windll.user32.EnumWindows(close_window, 0)
        child.wait(timeout=22)
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)
zip_path = options.zip_path.resolve() if options.zip_path else root.parent / "MediaTool-phase0-windows-x64.zip"
zip_path.parent.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
    for path in root.rglob("*"):
        if path.is_file():
            archive.write(path, path.relative_to(root.parent))
report = {
    "measurement": "Warm-cache process launch through native Qt off-screen first paint and event-loop callback. Not visible-window or post-reboot cold-start timing.",
    "runs": results,
    "median_launch_seconds": statistics.median(r["launch_seconds"] for r in results),
    "min_launch_seconds": min(r["launch_seconds"] for r in results),
    "max_launch_seconds": max(r["launch_seconds"] for r in results),
    "median_working_set_MiB": statistics.median(r['working_set_MiB'] for r in results),
    "median_private_bytes_MiB": statistics.median(r['private_bytes_MiB'] for r in results),
    "max_idle_cpu_percent_one_core": max(r['cpu_percent_one_core_scale'] for r in results),
    "idle_child_tools_absent": all(not r['child_processes'] for r in results),
    "optional_libraries_lazy": all(not r['loaded_optional_libraries'] for r in results),
    "installed_bytes": sum(size for _,size in inventory),
    "exe_bytes": exe.stat().st_size,
    "zip_bytes": zip_path.stat().st_size,
    "largest_files": inventory[:15],
    "external_media_tools_bundled": False,
    "Pillow_included": any("PIL" in name for name,_ in inventory),
    "cold_start": "Not measured; requires a separately recorded post-reboot run.",
    "windows11_linux_clean_machine": "Not tested on this Windows 10 host.",
}
options.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report, indent=2), flush=True)
