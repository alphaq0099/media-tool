"""Fixed external-tool catalog. No network activity or installers on import."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Dependency:
    key: str
    title: str
    purpose: str
    programs: tuple
    package: str
    download: str
    help: str


TOOLS=(
    Dependency('7zip','7-Zip','7z / encrypted archives',('7z',),'7zip.7zip',
        'https://www.7-zip.org/download.html','The 7-Zip command-line tool enables 7z and protected archives.'),
    Dependency('ffmpeg','FFmpeg','Video / audio',('ffmpeg','ffprobe'),'Gyan.FFmpeg.Essentials',
        'https://www.gyan.dev/ffmpeg/builds/','Installs Gyan’s Essentials build, including FFmpeg and FFprobe. Existing custom builds are retained. For a manual ZIP download, extract ffmpeg.exe and ffprobe.exe into the app’s tools folder or add their bin folder to PATH. The build determines which codecs are available.'),
    Dependency('tesseract','Tesseract','OCR',('tesseract',),'UB-Mannheim.TesseractOCR',
        'https://github.com/UB-Mannheim/tesseract/wiki','The Windows installer is maintained by UB Mannheim and linked from the Tesseract project. Choose the required language data in its installer. Documents checks the selected language before OCR.'),
    Dependency('libreoffice','LibreOffice','Office → PDF',('soffice',),'TheDocumentFoundation.LibreOffice',
        'https://www.libreoffice.org/download/','Installs the external Office suite. It is larger than the other tools and is only needed for Office-to-PDF conversion.'),
)
BY_KEY={tool.key:tool for tool in TOOLS}
PROBES={'winget':['--version'],'7z':['i'],'ffmpeg':['-version'],'ffprobe':['-version'],
    'tesseract':['--version'],'soffice':['--version']}


def install_arguments(key):
    tool=BY_KEY[key]
    return ['install','--id',tool.package,'--exact','--source','winget','--silent',
        '--disable-interactivity','--accept-source-agreements','--accept-package-agreements','--no-upgrade']
