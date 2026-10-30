"""Verify a local Windows ZIP and test its extracted app with a minimal PATH."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import zipfile

parser=argparse.ArgumentParser()
parser.add_argument('--zip',type=Path,required=True)
parser.add_argument('--package',type=Path,required=True)
parser.add_argument('--report',type=Path,required=True)
args=parser.parse_args()
report=args.report.resolve()
report.parent.mkdir(parents=True,exist_ok=True)
package=args.package.resolve()
expected={p.relative_to(package.parent).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
          for p in package.rglob('*') if p.is_file()}
with tempfile.TemporaryDirectory(prefix='rc-extract-space-',suffix='-日本',dir=report.parent) as scratch:
    destination=Path(scratch)
    with zipfile.ZipFile(args.zip) as archive:
        assert archive.testzip() is None
        names=archive.namelist()
        assert len(names)==len(set(names))
        assert set(names)==set(expected),'ZIP inventory differs from the package.'
        for name in names:
            path=destination/name
            assert path.resolve().is_relative_to(destination)
            archive.extract(name,destination)
            assert hashlib.sha256(path.read_bytes()).hexdigest()==expected[name],name
    env=os.environ.copy()
    for key in ('PYTHONPATH','PYTHONHOME','QT_PLUGIN_PATH','QML2_IMPORT_PATH','QT_QPA_PLATFORM'):
        env.pop(key,None)
    env['PATH']=str(Path(os.environ['SystemRoot'])/'System32')
    exe=destination/package.name/'MediaTool.exe'
    output=destination/'extracted.png'
    subprocess.run([str(exe),'--screenshot',str(output),'--sample-rows','10000',
        '--settings-file',str(destination/'settings.ini'),'--exit-after-ms','25000'],
        env=env,cwd=destination,check=True,timeout=35,stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
    assert output.is_file()
    docs=destination/package.name/'_internal'
    assert (docs/'README.md').is_file() and (docs/'docs/user-guide.md').is_file()
    assert not list(docs.rglob('roadmap.md'))
result=dict(passed=True,files_verified=len(expected),unicode_path=True,python_on_path=False,
    requested_sample_rows=10000,extracted_fixture_removed=not destination.exists(),
    limitations='Minimal PATH on this development PC; not a clean-machine or post-reboot test.')
report.write_text(json.dumps(result,indent=2),encoding='utf-8')
print(report.read_text())
