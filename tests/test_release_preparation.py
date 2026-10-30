"""Release source selection excludes private workspace material."""
import importlib.util
from pathlib import Path
import zipfile

import pytest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('prepare_release',ROOT/'scripts/prepare_release.py')
release=importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def test_public_selection_has_source_build_notices_and_no_internal_plans():
    paths=release.public_files()
    names={p.relative_to(ROOT).as_posix() for p in paths}
    release.audit_files(paths)
    assert {'LICENSE','MediaTool.spec','SOURCES.md','runtime-sources.json','src/media_tool/icons/media-tool.ico'}<=names
    assert not any(n.startswith(('artifacts/','prototypes/','.venv/','.git/')) for n in names)
    assert 'docs/roadmap.md' not in names and 'scripts/clean_workspace.ps1' not in names
    assert {n for n in names if n.startswith('docs/')}=={'docs/user-guide.md','docs/portable-readme.txt','docs/screenshots/workspace-dark.png'}


def test_credential_and_private_path_audit_rejects_files(tmp_path):
    path=tmp_path/'sample.py'
    for text in ('-----BEGIN '+'PRIVATE KEY-----','ghp_'+'x'*36,str(Path.home()/'private')):
        path.write_text(text)
        with pytest.raises(ValueError,match='Potential private'):release.audit_files([path],tmp_path)


def test_archive_has_one_root_and_keeps_unicode_names(tmp_path):
    root=tmp_path/'MediaTool-1.0.0'
    root.mkdir();(root/'日本.txt').write_text('fixture')
    target=tmp_path/'source.zip'
    release.archive_tree(root,target)
    with zipfile.ZipFile(target) as archive:
        assert archive.namelist()==['MediaTool-1.0.0/日本.txt']
        assert archive.read(archive.namelist()[0])==b'fixture'


def test_output_inside_package_is_rejected_before_writing(tmp_path):
    package=tmp_path/'artifacts/package'
    package.mkdir(parents=True)
    (package/'MediaTool.exe').write_bytes(b'fixture')
    with pytest.raises(ValueError,match='inside the application'):
        release.prepare(package,package/'release',tmp_path)
    assert not (package/'release').exists()
