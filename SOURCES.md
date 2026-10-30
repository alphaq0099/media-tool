# Source availability and rebuilding

Media Tool is GPL-3.0-or-later. Source repository:
https://github.com/alphaq0099/media-tool

The 1.0.0 release must publish `MediaTool-1.0.0-source.zip` beside
`MediaTool-1.0.0-windows-x64.zip` on the
[same release page](https://github.com/alphaq0099/media-tool/releases/tag/v1.0.0).
The matching Git tag is `v1.0.0`. The source archive contains the application,
icons, tests, build/CI scripts, pinned requirements and public documentation.
Internal development plans, user media, history and local build environments
are excluded.

## Rebuild the application

On Windows, install CPython 3.13.3 x64, unpack the source archive, then run:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock-windows.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm MediaTool.spec
```

The directory bundle is `dist/MediaTool`. The editable icon is
`src/media_tool/icons/media-tool.svg`; `scripts/build_icon.py` regenerates its ICO.
There is no activation key or lock preventing rebuilt application/library use.
Build timestamps, platform and tooling can affect bytes: the source/build
instructions support rebuilding, not a claim of bit-identical binaries.

## Upstream runtime source

The runtime libraries are upstream distributions. Their preferred source and
build instructions are available from the following providers. The publisher
must keep equivalent source access available alongside the release and update
these directions if a provider moves a source archive.

| Component | Matching source / build information |
| --- | --- |
| CPython 3.13.3 | [Source archive](https://www.python.org/ftp/python/3.13.3/Python-3.13.3.tgz); Windows build instructions and dependency fetch scripts are in `PCbuild`. |
| Qt 6.10.2 | [Official source archives](https://download.qt.io/official_releases/qt/6.10/6.10.2/submodules/): `qtbase`, `qtsvg` and `qtimageformats`, with their upstream build instructions. |
| PySide6 / Shiboken6 6.10.2 | [Official Qt for Python sources](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.10.2-src/) and [build documentation](https://doc.qt.io/qtforpython-6.10/building_from_source/index.html). |
| Mutagen 1.48.1 | [Source release](https://pypi.org/project/mutagen/1.48.1/#files) and [repository](https://github.com/quodlibet/mutagen). |
| Pillow 12.1.1 | [Source release](https://pypi.org/project/Pillow/12.1.1/#files). |
| pypdf 6.21.2 | [Source release](https://pypi.org/project/pypdf/6.21.2/#files). |
| pypdfium2 5.14.0 | [Source release](https://pypi.org/project/pypdfium2/5.14.0/#files); its included PDFium build/license notices identify the native build and sources. |
| cryptography 50.0.2 | [Source release](https://pypi.org/project/cryptography/50.0.2/#files); includes Rust build manifests and upstream OpenSSL build configuration. |
| cffi 2.1.1 | [Source release](https://pypi.org/project/cffi/2.1.1/#files). |
| pycparser 3.12 | [Source release](https://pypi.org/project/pycparser/3.12/#files). |
| PyInstaller 6.19.0 | [Source release](https://pypi.org/project/pyinstaller/6.19.0/#files), including bootloader source and its exception. |

Python package source locations and checksums are recorded during release
preparation in `runtime-sources.json`. The external media/OCR/Office executables
are not part of the base application package.
