# PyInstaller directory bundle. Keep only the imported Qt runtime.
from pathlib import Path
import sys
from importlib.metadata import distribution
project = Path(SPECPATH)
notices = []
for package in ("PySide6-Essentials", "shiboken6", "Pillow", "mutagen", "pypdf", "pyinstaller",
                "pypdfium2", "cryptography", "cffi", "pycparser"):
    dist = distribution(package)
    for item in dist.files or []:
        if any(token in str(item).lower() for token in ("license", "copying", "copyright", "notice")):
            source = Path(dist.locate_file(item))
            if source.is_file():
                notices.append((str(source), "licenses/" + package))
python_notice = Path(sys.base_prefix) / "LICENSE.txt"
if python_notice.is_file():
    notices.append((str(python_notice), "licenses/Python"))
a = Analysis(
    [str(project / "run.py")],
    pathex=[str(project / "src")],
    binaries=[], datas=notices + [(str(project / "README.md"), "."),
                                 (str(project / "LICENSE"), "."),
                                 (str(project / "LICENSES"), "licenses/upstream"),
                                 (str(project / "THIRD_PARTY_NOTICES.md"), "."),
                                 (str(project / "SOURCES.md"), "."),
                                 (str(project / "runtime-sources.json"), "."),
                                 (str(project / "CHANGELOG.md"), "."),
                                 (str(project / "SUPPORT.md"), "."),
                                 (str(project / "SECURITY.md"), "."),
                                 (str(project / "docs" / "portable-readme.txt"), "."),
                                 (str(project / "docs" / "user-guide.md"), "docs"),
                                 (str(project / "docs" / "screenshots" / "workspace-dark.png"), "docs/screenshots"),
                                 (str(project / "src" / "media_tool" / "icons"), "media_tool/icons")],
    hiddenimports=["PIL.Image"],
    hookspath=[str(project / "packaging" / "hooks")] if sys.platform == "linux" else [], hooksconfig={},
    excludes=["PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtWebEngineCore",
              "PySide6.QtWebEngineWidgets", "PySide6.QtMultimedia", "tkinter",
              "pytest", "psutil"],
    noarchive=False,
)
# This application uses only raster QWidget rendering, no QOpenGLWidget,
# Qt Quick or OpenGL context. Qt's optional software OpenGL renderer is unused.
# Revisit this exclusion if an OpenGL-backed view is added.
a.binaries = [entry for entry in a.binaries
              if Path(entry[0]).name.lower() != "opengl32sw.dll"]
# No QTranslator is loaded by the English-only workspace. Keep ICU (Unicode
# support); omit the unused Qt UI translation catalogs on Linux.
if sys.platform == "linux":
    a.datas = [entry for entry in a.datas
               if not entry[0].replace("\\", "/").startswith("PySide6/Qt/translations/")]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="MediaTool",
          icon=str(project / "src" / "media_tool" / "icons" / "media-tool.ico"),
          version=str(project / "packaging" / "windows-version-info.txt") if sys.platform=='win32' else None,
          debug=False, bootloader_ignore_signals=False, strip=sys.platform == "linux", upx=False,
          console=False, disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=sys.platform == "linux", upx=False, name="MediaTool")
