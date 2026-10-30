#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export DEBIAN_FRONTEND=noninteractive
sudo -n apt-get update -qq
sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  binutils libpython3.13 libegl1 libopengl0 libxkbcommon0 libxcb-cursor0 libxcb-icccm4 \
  libxcb-keysyms1 libxcb-shape0 libxcb-xinerama0 libxcb-xkb1 \
  libxkbcommon-x11-0 xvfb xauth fonts-dejavu-core ffmpeg 7zip \
  tesseract-ocr tesseract-ocr-eng libreoffice-writer libreoffice-calc libreoffice-impress
python3 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check -r requirements-dev.txt
mkdir -p artifacts
.venv/bin/python -m pip check
.venv/bin/python -m pip freeze > artifacts/linux-installed-packages.txt
.venv/bin/python - <<'PY'
import json,platform,sys,shutil
from pathlib import Path
report=dict(platform=platform.platform(),python=sys.version,
    tools={name:shutil.which(name) for name in ('ffmpeg','ffprobe','7z','7zz','tesseract','soffice','xvfb-run')})
Path('artifacts/linux-environment.json').write_text(json.dumps(report,indent=2))
print(json.dumps(report,indent=2))
PY
