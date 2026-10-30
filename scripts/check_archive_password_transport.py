"""Synthetic transport acceptance; optional --exe exercises the frozen worker.

The independent CLI check deliberately uses a generated test-only password.
Production command lines are observed separately and never saved.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import psutil

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / 'src'))
from media_tool.seven_archive import seven_run, remember_password
from media_tool.tool_paths import resolve_tool


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--exe', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    report = args.report.resolve()
    work = report.with_suffix('')
    work.mkdir(parents=True, exist_ok=False)
    source = work / 'source.bin'
    content = os.urandom(4 * 1024 * 1024)
    source.write_bytes(content)
    tool = resolve_tool('7z')
    old_executable = sys.executable
    if args.exe:
        sys.executable = str(args.exe.resolve())
        sys.frozen = True
    cases = []
    try:
        for index, password in enumerate((' Unicode 日本 € 🔒 $ & ; \\ trailing ', 'x' * 1024)):
            output = work / f'test-{index}.7z'
            stop = threading.Event()
            seen, leaks = set(), []
            def observe():
                while not stop.wait(.01):
                    for child in psutil.Process().children(recursive=True):
                        try:
                            argv = child.cmdline()
                            seen.add(child.name().lower())
                            if password in '\0'.join(argv):leaks.append(True)
                        except psutil.NoSuchProcess:
                            pass
            monitor = threading.Thread(target=observe)
            monitor.start()
            try:
                seven_run(['a', '-p', '-mhe=on', '-mx=1', '-mmt=2', '-bso0', '--', str(output), str(source)], password, lambda:False)
                data = seven_run(['x', '-so', '-bso0', '--', str(output)], password, lambda:False)
                assert data == content, 'Decrypted bytes differ.'
            finally:
                stop.set()
                monitor.join()
            assert not leaks, 'Password appeared in a production process argument.'
            assert '7z.exe' in seen, 'Observer did not see 7-Zip.'
            # Independent of the new transport. This password is synthetic.
            direct = subprocess.run([tool, 't', '-p'+password, '-bso0', '--', str(output)],
                capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
            assert direct.returncode == 0, 'Independent decryption rejected the password.'
            cases.append(dict(password_case='unicode-symbols-spaces' if index==0 else '1024-characters',
                              verified=True, production_argv_clean=True, observed_processes=sorted(seen)))
        for character in ('\b', '\t', '\x03', '\x7f'):
            try:remember_password('fixture'+character)
            except ValueError:pass
            else:raise AssertionError('Control character accepted.')
    finally:
        sys.executable = old_executable
        if args.exe:del sys.frozen
    report.write_text(json.dumps(dict(passed=True, frozen=bool(args.exe), cases=cases), indent=2))
    print(report.read_text())


if __name__ == '__main__':main()
