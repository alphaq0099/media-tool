# Release notes

## 1.0.0

First public release of Media Tool, following Windows user acceptance testing.

- Images: JPEG, PNG and WebP conversion, resizing and supported metadata options.
- Video: H.264/H.265/VP9 conversion, resize, target-size compression, selected
  audio/subtitles, external subtitles and compatible remux.
- Audio: conversion/extraction, trim/split/join, waveform editing, volume/loudness
  adjustment and supported tags/cover artwork.
- Documents: PDF page operations, image conversion, text extraction, lossless
  optimization or JPEG compression, metadata/passwords, optional OCR/Office tools.
- Rename and folders: batch naming, folder size inspection, flatten-one-level,
  previews and supported undo.
- Archives: 7z, ZIP and CBZ, combined/per-input modes and supported encryption.
- Queue: saved jobs and linked workflows with live progress, retry and history.
- Graphite dark/light interface, application icon, hand cursors and corrected
  Folders/Queue theme backgrounds.
- Support-report export that omits user filenames, paths and free-form logs.

Windows x64 is the first prebuilt target, tested on Windows 10. Windows 11,
clean-machine and assistive-technology acceptance remain unverified. Linux had
earlier Debian 13 source/packaged tests; no Linux binary is included here.

Python is included with the portable application. FFmpeg/FFprobe, 7-Zip,
Tesseract/languages and LibreOffice remain separate optional installations.

Quality-based conversion does not guarantee file size or retain source bitrate.
PDF recompression may not reduce an already optimized file. OCR accuracy and
Office layout depend on the input and installed tools. Advanced HDR/interlaced
video and animated/multipage image inputs have conversion limitations.
