Media Tool - local desktop media and file toolbox

Version 1.0.0. First Windows x64 portable release.

START
Extract the entire MediaTool folder. Launch MediaTool.exe on Windows or
./MediaTool on Linux. Keep _internal beside it. No separate Python install
is needed for packaged builds.

TOOLS
Images: JPEG/PNG/WebP conversion and resizing.
Video: H.264/H.265/VP9 conversion, resizing, compatible remux, selected
audio/subtitles and H.264/H.265 target-size compression in MP4/MKV.
Audio: conversion/extraction, trim/split/join, volume/loudness, tags/cover.
Rename: batch naming, preview and supported undo.
Archives: 7z/ZIP/CBZ, combined or per-input archives, optional encryption.
Documents: PDF page tools, images/PDF conversion, text, metadata/passwords,
optimization, and optional OCR/Office conversion.
Folders: inspection and flatten-one-level with supported undo.
Queue: saved jobs, linked tool workflows, retry and history.

FIRST OPERATION
1. Choose a tool in the sidebar.
2. Open/drop compatible inputs. Enable Subfolders if required.
3. Set the task, format, destination and options.
4. Review Preview, start directly, or add to Queue.
5. Follow live progress and outcomes in Results.

For most file tools, selected rows take priority; otherwise visible rows are
used. An empty destination saves beside inputs where supported. Use (i)
buttons for help. The bottom sidebar button switches light/dark theme.

OPTIONAL TOOLS
FFmpeg and FFprobe: video/audio operations and waveform analysis.
7-Zip: 7z and encrypted archives.
Tesseract plus language data: OCR.
LibreOffice: supported Office documents to PDF.

Tools > Dependencies checks availability and opens download pages. Windows
can install selected tools through WinGet after explicit agreement. Linux
users install them through their distribution/providers. External tools
are not bundled with the base application.

FILE HANDLING
Conversion keeps originals by default. Where offered, disabling Keep
originals requests moving verified inputs to Recycle Bin/Trash after success.
Rename and flatten move existing files. Review previews before applying.
Output conflicts use the chosen Skip/unique-name policy.

Pause waits for the current unit; Cancel requests stopping it. Retry and
undo depend on the operation and saved state. Changed files or occupied
destinations can block recovery/undo. Passwords are not saved as plaintext
in settings/history; protected jobs can require reentry. History contains
filenames and paths.

LIMITATIONS
Conversion supports selected progressive 8-bit SDR video; HDR/interlacing
and unsupported transforms may be rejected. Animated/multipage and unsupported
high-bit-depth images are rejected. Quality does not promise source bitrate
or a fixed output size. Scans need OCR for text. Office layout depends on
fonts/page setup. Metadata removal is not redaction. Conversion has no
universal undo.

DOCUMENTATION
Read README.md and docs/user-guide.md in the source repository for setup,
task instructions, workflows and troubleshooting. Third-party runtime
notices are under _internal/licenses and component metadata. For support,
use Queue > History > Tools > Export support report. Save report exports
the detailed job record and can contain private paths. Review shared samples.
