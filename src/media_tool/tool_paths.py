"""On-demand executable lookup without changing PATH or scanning whole drives."""
import os
from pathlib import Path
import shutil
import sys


def _seven_zip_registry():
    if os.name != "nt":
        return ()
    import winreg
    folders = []
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for key_name in (r"Software\7-Zip", r"Software\WOW6432Node\7-Zip"):
            try:
                with winreg.OpenKey(root, key_name) as key:
                    for value in ("Path64", "Path"):
                        try:
                            folder, _ = winreg.QueryValueEx(key, value)
                            if isinstance(folder, str):
                                folders.append(Path(os.path.expandvars(folder)))
                        except OSError:
                            pass
            except OSError:
                pass
    return folders


def _windows_path_folders():
    """Read newly registered PATH entries without modifying the process/system PATH."""
    if os.name!='nt':return ()
    import winreg
    folders=[]
    for hive,key in ((winreg.HKEY_LOCAL_MACHINE,r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment'),
                     (winreg.HKEY_CURRENT_USER,r'Environment')):
        try:
            with winreg.OpenKey(hive,key) as handle:value,_=winreg.QueryValueEx(handle,'Path')
            if isinstance(value,str):
                for raw in value.split(';'):
                    path=Path(os.path.expandvars(raw.strip().strip('"')))
                    if path.is_absolute():folders.append(path)
        except OSError:pass
    # WinGet's portable FFmpeg package exposes these links immediately, even
    # when this process inherited PATH before the package was installed.
    for variable in ('LOCALAPPDATA','ProgramFiles'):
        base=os.environ.get(variable)
        if base:folders.append(Path(base)/('Microsoft/WinGet/Links' if variable=='LOCALAPPDATA' else 'WinGet/Links'))
    return folders


def _document_folders(stem):
    if os.name!='nt':return ()
    relative={'tesseract':Path('Tesseract-OCR'),'soffice':Path('LibreOffice')/'program'}[stem]
    folders=[]
    for variable in ('ProgramW6432','ProgramFiles','ProgramFiles(x86)','LOCALAPPDATA'):
        base=os.environ.get(variable)
        if base:
            for folder in (Path(base),Path(base)/'Programs'):folders.append(folder/relative)
    return folders


def resolve_tool(name):
    found = shutil.which(name)
    if found:
        return found
    if Path(name).is_absolute() or Path(name).parent != Path("."):
        return None
    stem = Path(name).name.casefold().removesuffix(".exe")
    if stem not in ('ffmpeg','ffprobe','7z','7za','7zz','tesseract','soffice'):return None
    folders=[]
    if stem in ("ffmpeg", "ffprobe"):
        root = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2]
        filename = stem + (".exe" if os.name == "nt" else "")
        for folder in (root, root / "tools", root / "tools" / "bin"):
            candidate = folder / filename
            if candidate.is_file():
                return str(candidate)
    elif stem in ('tesseract','soffice'):
        folders.extend(_document_folders(stem))
    else:
        for alias in ("7z", "7zz", "7za"):
            found = shutil.which(alias)
            if found:return found
        folders.extend(_seven_zip_registry())
        if os.name == "nt":
            for variable in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
                folder = os.environ.get(variable)
                if folder:folders.append(Path(folder) / "7-Zip")
            local = os.environ.get("LOCALAPPDATA")
            if local:folders.append(Path(local) / "Programs" / "7-Zip")
    folders.extend(_windows_path_folders())
    filenames=('7z.exe','7zz.exe','7za.exe') if stem in ('7z','7za','7zz') else (stem+('.exe' if os.name=='nt' else ''),)
    for folder in dict.fromkeys(folders):
        for filename in filenames:
            candidate = folder / filename
            if candidate.is_file():
                return str(candidate)
    return None
