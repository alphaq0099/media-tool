"""Password privacy/restart and nested converter lifetime checks."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import psutil
import pytest
from PySide6.QtWidgets import QInputDialog,QLineEdit

from media_tool.documents import document_snapshot
from media_tool.private_values import remember,forget
from media_tool.domain import category
from media_tool.jobs import Cancelled
from media_tool.processes import run_process
from test_documents import make,pdf,execute
from test_foundation import app,window,wait_until


def test_saved_password_prompt_and_private_history(window,tmp_path,monkeypatch):
    password='Secret 日本 café !!'
    record=pdf(tmp_path/'input.pdf',1,password)
    token=remember(password)
    plan=make([record],document_task='extract',document_input_secret=token)
    queue=window.ensure_jobs()
    job=queue.store.enqueue('Protected document','documents',document_snapshot(plan))
    forget(token)
    prompts=[]
    def prompt(parent,title,message,echo):
        prompts.append((title,echo))
        return password,True
    monkeypatch.setattr(QInputDialog,'getText',prompt)
    queue.start_single(job)
    wait_until(lambda:queue.worker is None and not queue.enabled,40000)
    assert queue.store.items(job)[0]['state']=='succeeded',queue.store.items(job)
    assert prompts==[('Input PDF password',QLineEdit.EchoMode.Password)]
    assert password not in json.dumps(queue.store.items(job),ensure_ascii=False)
    assert password not in Path(window.settings.fileName()).read_text(encoding='utf-8') if Path(window.settings.fileName()).exists() else True
    queue.store.db.execute('PRAGMA wal_checkpoint(FULL)')
    for path in queue.store.directory.glob('history.sqlite3*'):
        assert password.encode('utf-8') not in path.read_bytes()
    forget(token)


def test_cancel_saved_password_prompt_leaves_job_waiting(window,tmp_path,monkeypatch):
    token=remember('fixture password')
    record=pdf(tmp_path/'locked.pdf',1,'fixture password')
    plan=make([record],document_input_secret=token)
    queue=window.ensure_jobs()
    job=queue.store.enqueue('Test','documents',document_snapshot(plan))
    forget(token)
    monkeypatch.setattr(QInputDialog,'getText',lambda *a:('',False))
    queue.start_single(job)
    assert not queue.enabled and queue.worker is None
    assert queue.store.items(job)[0]['state']=='queued'
    assert 'waiting for its password' in queue.status.text()


@pytest.mark.parametrize('suffix',['odt','ods','odp'])
def test_office_formats_use_documents_filter(suffix):
    assert category(Path('example.'+suffix))=='Document'


@pytest.mark.parametrize('stage',['created','written','prepared'])
def test_replaced_temp_is_kept_unchanged(tmp_path,stage):
    record=pdf(tmp_path/'source.pdf')
    plan=make([record])
    unrelated=tmp_path/'unrelated.txt'
    unrelated.write_bytes(b'Unrelated content')
    replacement=[]
    def hook(point):
        if point==stage:
            temp=next(tmp_path.glob('.mediatool-*.part'))
            temp.unlink()
            unrelated.rename(temp)
            replacement.append(temp)
    rows=execute(tmp_path,plan,hook)
    assert rows[0]['state']=='failed',rows
    assert replacement[0].read_bytes()==b'Unrelated content'
    assert not Path(plan.items[0].output).exists()


def test_replaced_temp_after_reader_verification_is_kept(tmp_path,monkeypatch):
    from media_tool import documents
    source=pdf(tmp_path/'source.pdf')
    plan=make([source])
    other=tmp_path/'other.pdf'
    other.write_bytes(Path(source.path).read_bytes())
    original=documents.invoke
    replaced=[]
    def invoke(request,*args,**kwargs):
        result=original(request,*args,**kwargs)
        if request['mode']=='verify':
            path=Path(request['output'])
            path.unlink()
            other.rename(path)
            replaced.append(path)
        return result
    monkeypatch.setattr(documents,'invoke',invoke)
    rows=execute(tmp_path,plan)
    assert rows[0]['state']=='failed' and 'ownership' in rows[0]['message']
    assert replaced[0].read_bytes()==Path(source.path).read_bytes()
    assert not Path(plan.items[0].output).exists()


def test_pdf_password_only_enters_private_stdin(tmp_path,monkeypatch):
    from media_tool import documents
    password='Private 日本 pass'
    source=pdf(tmp_path/'locked.pdf',1,password)
    original=documents.run_process
    calls=[]
    def process(arguments,*args,**kwargs):
        calls.append((arguments,kwargs.get('input_text','')))
        return original(arguments,*args,**kwargs)
    monkeypatch.setattr(documents,'run_process',process)
    plan=make([source],document_input_secret=remember(password))
    assert execute(tmp_path,plan)[0]['state']=='succeeded'
    assert calls and all(password not in ' '.join(args) for args,_ in calls)
    assert any(password in json.loads(data).get('secrets',{}).values() for _,data in calls)


@pytest.mark.parametrize('oriented',[False,True])
def test_image_orientation_transparency_and_jpeg_export(tmp_path,oriented):
    from PIL import Image
    import pypdfium2 as pdfium
    path=tmp_path/('rotated.jpg' if oriented else 'transparent.png')
    if oriented:
        image=Image.new('RGB',(60,30),'green')
        exif=Image.Exif()
        exif[274]=6
        image.save(path,exif=exif)
    else:Image.new('RGBA',(30,60),(255,0,0,0)).save(path)
    from media_tool.domain import FileRecord
    plan=make([FileRecord.from_path(path)],document_task='images_pdf',document_paper='image',document_dpi=72,document_margin=0)
    rows=execute(tmp_path,plan)
    assert rows[0]['state']=='succeeded',rows
    with pdfium.PdfDocument(plan.items[0].output) as doc:
        page=doc[0]
        assert page.get_size()==(30.,60.)
        bitmap=page.render(scale=1)
        actual=bitmap.to_pil().convert('RGB').getpixel((15,30))
        expected=(0,128,0) if oriented else (255,255,255)
        assert max(abs(a-b) for a,b in zip(actual,expected))<5
        bitmap.close()
        page.close()
    rendered=make([FileRecord.from_path(plan.items[0].output)],document_task='render',extension='jpg',document_dpi=144)
    assert execute(tmp_path,rendered)[0]['state']=='succeeded'
    with Image.open(rendered.items[0].output) as result:
        assert result.size==(60,120) and result.format=='JPEG'


@pytest.mark.skipif(os.name!='nt',reason='Windows Job Object descendant lifetime')
@pytest.mark.parametrize('parent_loss',[False,True])
def test_nested_launcher_processes_stop_on_cancel_or_parent_loss(tmp_path,parent_loss):
    marker=tmp_path/'descendant.pid'
    child_code="import os,sys,time;from pathlib import Path;p=Path(sys.argv[1]);q=p.with_suffix('.new');q.write_text(str(os.getpid()));q.replace(p);time.sleep(60)"
    launcher_code='import subprocess,sys,time;subprocess.Popen([sys.executable,"-c",sys.argv[1],sys.argv[2]]);time.sleep(60)'
    command=[sys.executable,'-c',launcher_code,child_code,str(marker)]
    if parent_loss:
        driver="""import os,sys,time,threading,json
from pathlib import Path
from media_tool.processes import run_process
marker=Path(sys.argv[1])
def disappear():
    while not marker.exists():time.sleep(.01)
    os._exit(17)
threading.Thread(target=disappear,daemon=True).start()
run_process(json.loads(sys.argv[2]))
"""
        result=subprocess.run([sys.executable,'-c',driver,str(marker),json.dumps(command)],timeout=20,
            env=dict(os.environ,PYTHONPATH=str(Path('src').resolve())),creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        assert result.returncode==17
    else:
        with pytest.raises(Cancelled):run_process(command,cancelled=marker.exists,timeout=15)
    pid=int(marker.read_text())
    try:psutil.Process(pid).wait(timeout=8)
    except psutil.NoSuchProcess:pass
    assert not psutil.pid_exists(pid)
