"""Prepare a local portable ZIP and an allowlisted, auditable public source tree."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import zipfile

ROOT=Path(__file__).resolve().parents[1]
PUBLIC_FILES=(
    '.gitignore','README.md','LICENSE','THIRD_PARTY_NOTICES.md','SOURCES.md',
    'CHANGELOG.md','SUPPORT.md','SECURITY.md','CONTRIBUTING.md','runtime-sources.json',
    'run.py','MediaTool.spec','pyproject.toml','requirements.txt','requirements-dev.txt',
    'requirements-lock-windows.txt','Start Media Tool.cmd',
    'docs/user-guide.md','docs/portable-readme.txt','docs/screenshots/workspace-dark.png',
)
PUBLIC_SCRIPTS=(
    'smoke_package.py','inventory_package.py','benchmark.py','benchmark_source.py',
    'build_icon.py','check_release_archive.py','check_archive_password_transport.py',
    'check_packaged_documents.py','linux_setup.sh','linux_tests.sh',
    'prepare_linux_bundle.py','measure_packaged_startup.py','prepare_release.py',
)


def public_files(root=ROOT):
    paths=[root/name for name in PUBLIC_FILES]
    paths.extend(root/'scripts'/name for name in PUBLIC_SCRIPTS)
    for name,extensions in (('src',{'.py','.svg','.ico'}),('tests',{'.py'}),
                            ('packaging',{'.py','.txt'}),('.github',{'.yml','.yaml'}),
                            ('LICENSES',None)):
        paths.extend(p for p in (root/name).rglob('*') if p.is_file()
                     and '__pycache__' not in p.parts and (extensions is None or p.suffix in extensions))
    for path in paths:
        if not path.is_file():raise ValueError('Missing public release file: '+str(path.relative_to(root)))
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):raise ValueError('Linked or external source file.')
    return sorted(set(paths))


def audit_files(paths,root=ROOT):
    forbidden={'artifacts','prototypes','.venv','.git','.agents','.codex','__pycache__'}
    profiles={str(Path.home())}
    parts=root.resolve().parts
    for i,part in enumerate(parts[:-1]):
        if part.casefold()=='users':profiles.add(str(Path(*parts[:i+2])))
    private_paths={variant.encode().lower() for profile in profiles
                   for variant in (profile,profile.replace('\\','/'),json.dumps(profile)[1:-1])}
    patterns=[re.compile(rb'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----'),
              re.compile(rb'\bgh[pousr]_[A-Za-z0-9]{30,}\b'),
              re.compile(rb'\bgithub_pat_[A-Za-z0-9_]{40,}\b'),
              re.compile(rb'\bAKIA[0-9A-Z]{16}\b')]
    for path in paths:
        relative=path.relative_to(root)
        if set(relative.parts)&forbidden or path.suffix in ('.sqlite3','.db','.ini','.pstats'):
            raise ValueError('Private/local file in source selection: '+str(relative))
        if path.suffix.lower() not in ('.png','.ico'):
            data=path.read_bytes()
            if any(needle in data.lower() for needle in private_paths) or any(p.search(data) for p in patterns):
                raise ValueError('Potential private path or credential in '+str(relative))


def sha256(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def archive_tree(folder,target):
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        for path in sorted(folder.rglob('*')):
            if path.is_file():archive.write(path,path.relative_to(folder.parent).as_posix())


def prepare(package,output,root=ROOT):
    import sys
    sys.path.insert(0,str(root/'src'))
    from media_tool import __version__
    if not re.fullmatch(r'\d+\.\d+\.\d+',__version__):raise ValueError('Expected a final release version.')
    package,output=package.resolve(),output.resolve()
    if not package.is_dir() or not (package/'MediaTool.exe').is_file():raise ValueError('Provide the complete Windows package folder.')
    if output.exists():raise FileExistsError('Use a new release directory; existing release files are preserved.')
    if output.is_relative_to(package):raise ValueError('Release output cannot be inside the application package.')
    if not output.is_relative_to((root/'artifacts').resolve()):raise ValueError('Release output must stay under project artifacts.')
    for path in package.rglob('*'):
        if path.is_symlink() or not path.resolve().is_relative_to(package):raise ValueError('Linked package entry.')
        if path.is_file() and path.suffix in ('.sqlite3','.db','.ini','.pstats'):raise ValueError('Local state in package.')
    paths=public_files(root)
    audit_files(paths,root)
    output.mkdir(parents=True)
    source=output/'source'/f'MediaTool-{__version__}'
    for path in paths:
        destination=source/path.relative_to(root)
        destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(path,destination)
    source_zip=output/f'MediaTool-{__version__}-source.zip'
    binary_zip=output/f'MediaTool-{__version__}-windows-x64.zip'
    archive_tree(source,source_zip)
    archive_tree(package,binary_zip)
    artifacts=[binary_zip,source_zip]
    checksums=''.join(f'{sha256(p)}  {p.name}\n' for p in artifacts)
    (output/'SHA256SUMS.txt').write_text(checksums,encoding='ascii')
    shutil.copyfile(root/'CHANGELOG.md',output/'RELEASE_NOTES.md')
    file_hashes={p.relative_to(root).as_posix():sha256(p) for p in paths}
    manifest=dict(version=__version__,repository='https://github.com/alphaq0099/media-tool',
        source_files=file_hashes,
        package_files={p.relative_to(package).as_posix():sha256(p) for p in sorted(package.rglob('*')) if p.is_file()},
        artifacts={p.name:dict(bytes=p.stat().st_size,sha256=sha256(p)) for p in artifacts},
        published=False,git_operations_performed=False,
        audit='Allowlisted files and obvious credential/private-path patterns; not a guarantee that all possible secrets can be detected.')
    (output/'release-manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    print(json.dumps(dict(output=str(output),source_files=len(paths),artifacts=manifest['artifacts']),indent=2))
    return manifest


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    prepare(args.package,args.output)
