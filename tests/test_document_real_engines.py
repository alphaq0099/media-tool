"""Small end-to-end checks against separately installed document engines."""
from pathlib import Path
import zipfile

import pytest
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader

from media_tool.document_external import available_tools
from media_tool.domain import FileRecord
from test_documents import make, execute


@pytest.mark.skipif(not available_tools()['tesseract'], reason='Optional Tesseract unavailable')
@pytest.mark.parametrize('extension', ['txt', 'pdf'])
def test_real_ocr_recovers_generated_text(tmp_path, extension):
    source = tmp_path / 'scan.png'
    image = Image.new('RGB', (1600, 300), 'white')
    font = ImageFont.load_default(size=48)
    ImageDraw.Draw(image).text((60, 100), 'Media Tool document acceptance 12345', font=font, fill='black')
    image.save(source, dpi=(150, 150))
    before = source.read_bytes()
    plan = make([FileRecord.from_path(source)], document_task='ocr', extension=extension)
    rows = execute(tmp_path, plan)
    assert rows[0]['state'] == 'succeeded', rows[0]['message']
    output = Path(plan.items[0].output)
    text = output.read_text(encoding='utf-8') if extension == 'txt' else PdfReader(output).pages[0].extract_text()
    assert 'acceptance' in text and '12345' in text, text
    assert source.read_bytes() == before
    assert not list((tmp_path / 'history').glob('.mediatool-doc-*'))


@pytest.mark.skipif(not available_tools()['soffice'], reason='Optional LibreOffice unavailable')
def test_real_office_conversion_preserves_text(tmp_path):
    source = tmp_path / 'document.odt'
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('mimetype', 'application/vnd.oasis.opendocument.text')
        archive.writestr('content.xml', '''<?xml version="1.0"?><office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" office:version="1.2"><office:body><office:text><text:p>Media Tool document acceptance 12345</text:p></office:text></office:body></office:document-content>''')
        archive.writestr('META-INF/manifest.xml', '''<?xml version="1.0"?><manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"><manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.text"/><manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/></manifest:manifest>''')
    before = source.read_bytes()
    plan = make([FileRecord.from_path(source)], document_task='office')
    rows = execute(tmp_path, plan)
    assert rows[0]['state'] == 'succeeded', rows[0]['message']
    assert 'acceptance 12345' in PdfReader(plan.items[0].output).pages[0].extract_text()
    assert source.read_bytes() == before
    assert not list((tmp_path / 'history').glob('.mediatool-doc-*'))
