"""Diagnose password stdin using synthetic data only."""
import subprocess
import tempfile
from pathlib import Path

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    (root / 'input.txt').write_text('generated fixture')
    for repeats in (1, 2):
        result = subprocess.run(['7z', 'a', '-p', '-mhe=on', str(root / f'{repeats}.7z'), str(root / 'input.txt')],
            input=b'fixture-only-password\n' * repeats, capture_output=True, timeout=10)
        print(repeats, result.returncode, result.stdout.decode(errors='replace'), result.stderr.decode(errors='replace'))
        for action in (['l', '-slt', '-ba'], ['t', '-bso0', '-bsp0'], ['x', '-so', '-bso0', '-bsp0']):
            result = subprocess.run(['7z', *action, '--', str(root / f'{repeats}.7z')], input=b'fixture-only-password\n', capture_output=True, timeout=10)
            print(action, result.returncode, repr(result.stdout), repr(result.stderr))
