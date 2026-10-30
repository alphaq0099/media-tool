"""Keep Qt's normal dependency collection, without the unused GTK theme."""
import sys
from pathlib import Path
from PyInstaller.utils.hooks.qt import add_qt6_dependencies

hiddenimports, binaries, datas = add_qt6_dependencies(__file__)
if sys.platform == 'linux':
    binaries = [(source, destination) for source, destination in binaries
                if Path(source).name != 'libqgtk3.so']
