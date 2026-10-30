# Third-party notices

Media Tool 1.0.0 is GPL-3.0-or-later. Bundled components retain their own licenses.
They are unmodified upstream distributions, selected and packaged with
PyInstaller. License texts are included under `licenses/` in the portable app's
`_internal` directory. The source repository also contains the additional
Qt/GPL/LGPL notices under `LICENSES/`.

| Component | Version | Upstream license / notice |
| --- | --- | --- |
| CPython | 3.13.3 in the Windows release | Python Software Foundation and embedded third-party notices |
| PySide6 Essentials / Shiboken6 / Qt | 6.10.2 | LGPLv3/GPL alternatives; Qt's third-party notices also apply |
| Pillow | 12.1.1 | MIT-CMU and bundled dependency notices |
| Mutagen | 1.48.1 | GPL-2.0-or-later |
| pypdf | 6.21.2 | BSD-3-Clause |
| pypdfium2 | 5.14.0 | BSD-3-Clause / Apache-2.0 and PDFium build notices |
| cryptography | 50.0.2 | Apache-2.0 or BSD-3-Clause; upstream native dependency notices apply |
| cffi | 2.1.1 | MIT-0 |
| pycparser | 3.12 | BSD-3-Clause |
| PyInstaller bootloader | 6.19.0 | GPL with the PyInstaller bootloader distribution exception |

The packaged Qt modules are Core, Gui, Network, Svg and Widgets, with their
platform/image/icon plugins. The open-source GPL/LGPL texts and upstream Qt
component attribution/license files are included. The commercial-license
reference supplied in the PySide wheel is preserved as upstream metadata; this
release does not claim a Qt commercial license. Shared Qt libraries are supplied
as separate files; their replacement or rebuilding is not technically prevented.

The Pillow, PDFium, Python and cryptography distributions include additional
native components and licenses. Read their full included notice files, not just
the abbreviated table above. Windows runtime components retain their providers'
terms. Build-tool dependencies that are not part of the runtime are recorded in
`requirements-lock-windows.txt`.

FFmpeg/FFprobe, 7-Zip, Tesseract/language data and LibreOffice are separate
installations. They are not included in the base ZIP and retain their providers'
licenses. The dependency screen opens provider pages or invokes the selected
package manager after the user requests installation.

See [source availability and rebuilding](SOURCES.md). Qt source notice provenance
and hashes are recorded in `LICENSES/Qt-6.10.2/SOURCE-MANIFEST.json`.
