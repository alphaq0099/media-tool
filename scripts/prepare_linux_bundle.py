"""Allowlisted cloud test upload: source and synthetic tests, without user data."""
import hashlib
import json
from pathlib import Path
import tarfile

root=Path(__file__).resolve().parents[1]
out=root/'artifacts'/'linux-instabox'
out.mkdir(parents=True,exist_ok=True)
files=[]
for folder,extensions in (('src',{'.py','.svg','.ico'}),('tests',{'.py'}),('packaging',{'.py','.txt'})):
    files.extend(p for p in (root/folder).rglob('*') if p.is_file() and p.suffix in extensions and '__pycache__' not in p.parts)
names=['run.py','MediaTool.spec','pyproject.toml','requirements.txt','requirements-dev.txt','README.md',
    'LICENSE','THIRD_PARTY_NOTICES.md','SOURCES.md','CHANGELOG.md','SUPPORT.md','SECURITY.md','runtime-sources.json',
    'docs/portable-readme.txt','docs/user-guide.md','docs/screenshots/workspace-dark.png',
    'scripts/linux_setup.sh','scripts/linux_tests.sh',
    'scripts/smoke_package.py','scripts/inventory_package.py','scripts/check_packaged_documents.py',
    'scripts/measure_packaged_startup.py']
files.extend(root/name for name in names)
files.extend(p for p in (root/'LICENSES').rglob('*') if p.is_file())
archive=out/'source-tests.tgz'
with tarfile.open(archive,'w:gz') as tar:
    for path in sorted(files):
        if path.is_symlink() or not path.resolve().is_relative_to(root):raise ValueError('Refusing linked or external upload.')
        tar.add(path,arcname=path.relative_to(root).as_posix(),recursive=False)
with archive.open('rb') as handle:sha=hashlib.file_digest(handle,'sha256').hexdigest()
manifest=dict(archive=archive.name,sha256=sha,bytes=archive.stat().st_size,
    files=[p.relative_to(root).as_posix() for p in sorted(files)],
    excluded=['local artifacts/history/settings','build/dist','venv/caches','prototype/media files','credentials/conversations'])
(out/'upload-manifest.json').write_text(json.dumps(manifest,indent=2))
print(json.dumps(dict(archive=str(archive),bytes=archive.stat().st_size,sha256=sha,files=len(files)),indent=2))
