"""Optional tool contracts without installing engines or language packs."""
from pathlib import Path
import json
import shutil
import zipfile

import pytest
from pypdf import PdfReader

from media_tool import document_external as external
from media_tool import documents
from media_tool.jobs import Store, execute_job, Cancelled
from media_tool.domain import FileRecord
from media_tool.planning import PlanRequest
from test_documents import pdf, make, execute, text_pdf
from test_foundation import app


def office(path, content='<document/>'):
    with zipfile.ZipFile(path,'w') as archive:
        archive.writestr('content.xml',content)
    return FileRecord.from_path(path)


def test_missing_tools_and_languages_are_actionable(tmp_path,monkeypatch):
    monkeypatch.setattr(external,'available_tools',lambda:dict(tesseract=None,soffice=None))
    for task,record in [('ocr',pdf(tmp_path/'scan.pdf')),('office',office(tmp_path/'doc.odt'))]:
        with pytest.raises(ValueError,match='Install'):
            make([record],document_task=task)
    executable=tmp_path/'fake.exe'
    executable.write_bytes(b'fixture')
    monkeypatch.setattr(external,'available_tools',lambda:dict(tesseract=str(executable),soffice=None))
    monkeypatch.setattr(external,'run_process',lambda *a,**k:'List of available languages (1):\neng\n')
    with pytest.raises(ValueError,match='hin'):
        make([FileRecord.from_path(tmp_path/'scan.pdf')],document_task='ocr',document_language='eng+hin')
    with pytest.raises(ValueError,match='language codes'):
        external.prepare_external(dict(task='ocr',language='../eng',extension='pdf'),lambda:False)


@pytest.mark.parametrize('content',[
    '<Relationship TargetMode="External"/>', '<a xlink:href="https://example.test/data"/>',
    '<encryption-data/>', '<a href="macro:bad"/>'])
def test_office_external_content_rejected(tmp_path,content):
    record=office(tmp_path/'input.odt',content)
    result=make([record],document_task='office')
    assert result.items[0].status=='Invalid'


def test_owned_scope_cleanup_preserves_unrelated_files(tmp_path):
    store=Store(tmp_path/'history')
    try:
        plan=make([pdf(tmp_path/'source.pdf')])
        job=store.enqueue('Test','documents',documents.document_snapshot(plan))
        item=store.items(job)[0]
        other=store.directory/'user-file.txt'
        other.write_text('keep')
        scope=external.create_scope(store,item['id'])
        root=Path(scope['path'])
        (root/'nested').mkdir()
        (root/'nested'/'scratch').write_bytes(b'generated')
        external.cleanup_scope(store,item['id'])
        assert not root.exists() and other.read_text()=='keep'
        scope=external.create_scope(store,item['id'])
        root=Path(scope['path'])
        (root/'.owner').write_text('replaced')
        with pytest.raises(ValueError,match='marker changed'):
            external.cleanup_scope(store,item['id'])
        assert root.exists()
    finally:store.close()


@pytest.mark.parametrize('task,extension,encrypted',[('office','pdf',False),('ocr','pdf',False),('ocr','txt',False),('ocr','pdf',True)])
def test_external_protocol_outputs_verified_and_scope_removed(tmp_path,monkeypatch,app,task,extension,encrypted):
    executable=tmp_path/'fake.exe'
    executable.write_bytes(b'adapter fixture, never executed')
    monkeypatch.setattr(external,'available_tools',lambda:dict(tesseract=str(executable),soffice=str(executable)))
    calls=[]
    def process(args,*a,**k):
        calls.append(args)
        if '--list-langs' in args:return 'List of available languages (1):\neng\n'
        if '--version' in args:return 'LibreOffice fixture'
        if '--convert-to' in args:
            destination=Path(args[args.index('--outdir')+1])/(Path(args[-1]).stem+'.pdf')
            text_pdf(destination,('Recognized example',))
        else:
            destination=Path(args[2]).with_suffix('.'+args[-1])
            if args[-1]=='pdf':text_pdf(destination,('Recognized example',))
            else:destination.write_text('Recognized example',encoding='utf-8')
        return ''
    monkeypatch.setattr(external,'run_process',process)
    original_invoke=documents.invoke
    def invoke(request,cancelled=lambda:False,progress=None):
        if request['mode']=='run':
            from media_tool.pdf_backend import dispatch
            return dispatch(request)
        return original_invoke(request,cancelled,progress)
    monkeypatch.setattr(documents,'invoke',invoke)
    password='OCR 日本 password' if encrypted else ''
    record=office(tmp_path/'document.odt') if task=='office' else pdf(tmp_path/'scan.pdf',1,password)
    before=Path(record.path).read_bytes()
    from media_tool.private_values import remember
    plan=make([record],document_task=task,extension=extension,document_input_secret=remember(password) if encrypted else '')
    rows=execute(tmp_path,plan)
    assert rows[0]['state']=='succeeded',rows
    assert Path(plan.items[0].output).is_file()
    assert not list((tmp_path/'history').glob('.mediatool-doc-*'))
    assert Path(record.path).read_bytes()==before
    assert any('--convert-to' in c if task=='office' else '-l' in c for c in calls)
    assert not rows[0]['stage']
    if encrypted:
        result=PdfReader(plan.items[0].output)
        assert result.is_encrypted and result.decrypt(password)


def test_external_failure_cleans_workspace(tmp_path,monkeypatch):
    source=pdf(tmp_path/'source.pdf')
    monkeypatch.setattr(external,'prepare_external',lambda *a:{})
    plan=make([source],document_task='ocr')
    original=documents.invoke
    def fail(request,*args):
        if request['mode']=='run':
            (Path(request['spec']['scratch'])/'partial').write_bytes(b'partial')
            raise Cancelled('Cancelled fixture')
        return original(request,*args)
    monkeypatch.setattr(documents,'invoke',fail)
    rows=execute(tmp_path,plan)
    assert rows[0]['state']=='cancelled'
    assert not Path(plan.items[0].output).exists()
    assert not list((tmp_path/'history').glob('.mediatool-doc-*'))


@pytest.mark.parametrize('point',['created','prepared','committed'])
def test_pdf_interruption_retry(tmp_path,point):
    record=pdf(tmp_path/'source.pdf')
    plan=make([record],document_task='extract',document_pages='2')
    store=Store(tmp_path/'history')
    class Crash(BaseException):pass
    def hook(stage):
        if stage==point:raise Crash()
    try:
        job=store.enqueue('Test','documents',documents.document_snapshot(plan))
        with pytest.raises(Crash):execute_job(store,job,hook=hook)
        store.recover()
        assert store.retry(job)==1
        execute_job(store,job)
        assert store.items(job)[0]['state']=='succeeded',store.items(job)
        assert len(PdfReader(plan.items[0].output).pages)==1
    finally:store.close()


@pytest.mark.skipif(not external.available_tools()['tesseract'],reason='Tesseract not installed; real-engine acceptance remains open')
def test_real_tesseract_language_probe():
    result=external.prepare_external(dict(task='ocr',extension='txt',language='eng'),lambda:False)
    assert Path(result['external_tool']).is_file()


@pytest.mark.skipif(not external.available_tools()['soffice'],reason='LibreOffice not installed; real-engine acceptance remains open')
def test_real_office_version_probe():
    result=external.prepare_external(dict(task='office'),lambda:False)
    assert Path(result['external_tool']).is_file()
