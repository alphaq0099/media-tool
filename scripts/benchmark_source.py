"""Warm source startup measurements using the actual GUI PID, not the venv launcher."""
import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time

import psutil


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    report = args.report.resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    runs = []
    with tempfile.TemporaryDirectory(prefix="source-startup-", dir=report.parent) as scratch:
        base = Path(scratch)
        for iteration in range(4):
            marker = base / f"ready-{iteration}.json"
            settings = base / f"settings-{iteration}.ini"
            started = time.perf_counter()
            child = subprocess.Popen([sys.executable, str(root / "run.py"), "--settings-file", str(settings),
                                      "--benchmark-ready", str(marker), "--exit-after-ms", "3500"],
                                     cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                while True:
                    if child.poll() is not None:
                        raise RuntimeError(child.stderr.read().decode(errors="replace"))
                    if time.perf_counter() - started > 30:
                        raise TimeoutError("GUI startup timed out")
                    try:
                        ready = json.loads(marker.read_text(encoding="utf-8"))
                        break
                    except (FileNotFoundError, PermissionError, json.JSONDecodeError):
                        time.sleep(.005)
                process = psutil.Process(ready["pid"])
                time.sleep(.25)
                memory = process.memory_info()
                children = [p.name() for p in process.children(recursive=True)]
                run = dict(iteration=iteration, warmup=iteration == 0, actual_gui_pid=ready["pid"],
                           launcher_pid=child.pid, launch_seconds=round(ready["ready_perf_counter"] - started, 4),
                           working_set_MiB=round(memory.rss / 2**20, 2),
                           private_bytes_MiB=round(getattr(memory, "private", memory.vms) / 2**20, 2),
                           idle_cpu_percent_one_core=process.cpu_percent(interval=.5),
                           child_processes=children, platform=ready["qt_platform"],
                           loaded_optional_libraries=ready.get("loaded_optional_libraries", []))
                runs.append(run)
                print(json.dumps(run), flush=True)
                child.wait(timeout=10)
                assert child.returncode == 0
            finally:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=10)
                child.stderr.close()
    warm = runs[1:]
    result = dict(passed=True, kind="warm source launch; one warmup excluded", runs=runs,
                  median_launch_seconds=statistics.median(r["launch_seconds"] for r in warm),
                  median_working_set_MiB=statistics.median(r["working_set_MiB"] for r in warm),
                  no_child_tools_when_idle=all(not r["child_processes"] for r in warm),
                  optional_libraries_not_loaded=all(not r["loaded_optional_libraries"] for r in warm),
                  fixture_removed=not base.exists(),
                  note="Actual GUI PID is reported by the app. This is not a cold-start, frozen-package or clean-machine measurement.")
    report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "runs"}, indent=2))


if __name__ == "__main__":
    main()
