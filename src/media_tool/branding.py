"""Shared application/window icon; keep artwork outside the runtime code."""
from pathlib import Path
from PySide6.QtGui import QIcon


def application_icon():
    return QIcon(str(Path(__file__).with_name('icons')/'media-tool.ico'))
