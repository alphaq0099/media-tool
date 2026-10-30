#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p artifacts
export QT_QPA_PLATFORM=offscreen
export PYTHONUNBUFFERED=1
export OMP_NUM_THREADS=2
export OPENBLAS_NUM_THREADS=2
.venv/bin/python -m pytest -q -ra -p no:cacheprovider \
  --basetemp "$PWD/linux-test-fixtures" --junitxml=artifacts/linux-pytest-initial.xml
