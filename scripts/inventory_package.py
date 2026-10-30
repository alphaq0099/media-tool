"""Measure actual package files without double-counting symbolic links."""
import argparse
from collections import Counter
import json
from pathlib import Path
import tarfile

parser = argparse.ArgumentParser()
parser.add_argument('--package', type=Path, required=True)
parser.add_argument('--report', type=Path, required=True)
parser.add_argument('--archive', type=Path)
args = parser.parse_args()
root = args.package.resolve(strict=True)
files = [p for p in root.rglob('*') if p.is_file() and not p.is_symlink()]
groups = Counter()
for path in files:
    relative = path.relative_to(root)
    group = relative.parts[1] if relative.parts[0] == '_internal' and len(relative.parts) > 1 else relative.parts[0]
    groups[group] += path.stat().st_size
result = dict(bytes=sum(p.stat().st_size for p in files), files=len(files),
    symlinks=sum(p.is_symlink() for p in root.rglob('*')),
    largest=sorted([(p.stat().st_size, p.relative_to(root).as_posix()) for p in files], reverse=True)[:20],
    groups=dict(groups.most_common()))
if args.archive:
    with tarfile.open(args.archive, 'w:gz', dereference=False) as archive:
        archive.add(root, arcname=root.name)
    result['archive_bytes'] = args.archive.stat().st_size
args.report.parent.mkdir(parents=True, exist_ok=True)
args.report.write_text(json.dumps(result, indent=2), encoding='utf-8')
print(json.dumps(result, indent=2))
