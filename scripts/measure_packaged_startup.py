"""Warm native-platform first-paint measurements with isolated settings."""
import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess
import time

import psutil

parser = argparse.ArgumentParser()
parser.add_argument('--exe', type=Path, required=True)
parser.add_argument('--report', type=Path, required=True)
args = parser.parse_args()
report = args.report.resolve()
work = report.with_suffix('')
work.mkdir(parents=True, exist_ok=False)
cases = []
for index in range(3):
    ready = work / f'ready-{index}.json'
    started = time.perf_counter()
    process = subprocess.Popen([str(args.exe.resolve()), '--benchmark-ready', str(ready),
        '--settings-file', str(work / f'{index}.ini'), '--exit-after-ms', '1600'],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        while not ready.exists():
            if process.poll() is not None or time.perf_counter() - started > 15:
                raise RuntimeError('Packaged startup did not become ready.')
            time.sleep(.01)
        data = json.loads(ready.read_text())
        time.sleep(.3)
        rss = psutil.Process(data['pid']).memory_info().rss
        assert process.wait(timeout=15) == 0
        cases.append(dict(first_paint_seconds=data['ready_perf_counter'] - started,
                          rss_bytes=rss, platform=data['qt_platform'],
                          loaded_optional_libraries=data['loaded_optional_libraries']))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
result = dict(mode='Warm native-platform off-screen first paint; RSS 300 ms after ready; three runs.',
              limitations='Virtual display on the Linux test box; not cold startup or measurements on the Windows PC.',
              median_seconds=statistics.median(c['first_paint_seconds'] for c in cases), cases=cases)
report.write_text(json.dumps(result, indent=2))
print(report.read_text())
