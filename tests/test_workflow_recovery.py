from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from media_tool.jobs import Store,execute_job,identity
from media_tool.workflows import workflow_snapshot
from test_workflows import step,image,enqueue,outputs
from test_foundation import app,window,wait_until


@pytest.mark.parametrize('point,count',[('workflow_planned',1),('committed',2),('workflow_child_finished',2)])
def test_real_process_loss_after_durable_handoff(tmp_path,point,count):
    source=image(tmp_path/'source.png')
    history=tmp_path/'history'
    store=Store(history)
    job=enqueue(store,[source],[step('images',extension='jpg'),step('rename',prefix='Final_'),step('archives')])
    store.close()
    driver='''import os,sys
from media_tool.jobs import Store,execute_job
store=Store(sys.argv[1])
count=0
def hook(point):
    global count
    if point==sys.argv[3]:
        count+=1
        if count==int(sys.argv[4]):os._exit(27)
execute_job(store,sys.argv[2],hook=hook)
'''
    result=subprocess.run([sys.executable,'-c',driver,str(history),job,point,str(count)],timeout=40,
        env=dict(os.environ,PYTHONPATH=str(Path('src').resolve())),creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    assert result.returncode==27
    store=Store(history)
    try:
        store.recover()
        assert store.visible_jobs()[0]['state']=='interrupted'
        store.retry(job)
        assert execute_job(store,job)=='succeeded',store.items(job)
        assert len(store.jobs())==4
        assert len(list(tmp_path.glob('Final_*.jpg')))==1
        assert len(list(tmp_path.glob('Archive*.zip')))==1
    finally:store.close()


def test_same_identity_content_change_blocks_rename(tmp_path):
    source=image(tmp_path/'source.png')
    store=Store(tmp_path/'history')
    def hook(point):
        if point=='workflow_checkpoint' and store.items(job)[1]['state']=='queued':
            first=Path(json.loads(store.items(job)[0]['stage'])['outputs'][0]['source'])
            info=first.stat()
            data=bytearray(first.read_bytes())
            data[-1]^=1
            first.write_bytes(data)
            os.utime(first,ns=(info.st_atime_ns,info.st_mtime_ns))
            assert identity(first.stat())==identity(info)
    try:
        job=enqueue(store,[source],[step('images',extension='jpg'),step('rename'),step('archives')])
        execute_job(store,job,hook=hook)
        assert [r['state'] for r in store.items(job)]==['succeeded','failed','blocked']
        assert 'changed' in store.items(job)[1]['message']
        assert not list(tmp_path.glob('File_*.jpg'))
    finally:store.close()


def test_cancelled_child_does_not_escape_parent_scheduling(tmp_path):
    source=image(tmp_path/'source.png')
    store=Store(tmp_path/'history')
    class Crash(BaseException):pass
    def hook(point):
        if point=='workflow_planned':raise Crash()
    try:
        job=enqueue(store,[source],[step('images',extension='jpg'),step('archives')])
        with pytest.raises(Crash):execute_job(store,job,hook=hook)
        store.recover()
        child=json.loads(store.items(job)[0]['stage'])['child_job']
        assert next(j for j in store.jobs() if j['id']==child)['state']=='queued'
        assert all(j['id']!=child for j in store.visible_jobs())
        assert store.visible_jobs()[0]['state']=='interrupted'
    finally:store.close()


def test_separate_archives_and_multiple_outputs(tmp_path):
    records=[image(tmp_path/'two.png'),image(tmp_path/'one.png')]
    store=Store(tmp_path/'history')
    try:
        job=enqueue(store,records,[step('images',extension='jpg'),step('rename',prefix='Part_'),step('archives',archive_mode='separate')])
        assert execute_job(store,job)=='succeeded',store.items(job)
        files=outputs(store,job)
        assert len(files)==2
        for artifact in files:
            with zipfile.ZipFile(artifact['source']) as archive:assert len(archive.namelist())==1
    finally:store.close()


def test_workflow_password_reentry_and_saved_child_privacy(window,tmp_path,monkeypatch):
    from PySide6.QtWidgets import QInputDialog
    from media_tool.seven_archive import forget_password,remember_password
    source=image(tmp_path/'source.png')
    secret='Workflow 日本 pass'
    token=remember_password(secret)
    request=step('archives',archive_format='7z',archive_encrypt=True)
    request['archive_secret']=token
    queue=window.ensure_jobs()
    job=enqueue(queue.store,[source],[request])
    forget_password(token)
    prompts=[]
    def prompt(*args):
        prompts.append(True)
        return secret,True
    monkeypatch.setattr(QInputDialog,'getText',prompt)
    queue.start_single(job)
    wait_until(lambda:queue.worker is None and not queue.enabled,30000)
    assert queue.saved_jobs()[-1]['state']=='succeeded',queue.store.items(job)
    assert prompts
    child=json.loads(queue.store.items(job)[0]['stage'])['child_job']
    assert secret not in json.dumps(queue.store.items(job)+queue.store.items(child),ensure_ascii=False)
    forget_password(token)


def test_history20_migration_preserves_saved_jobs(tmp_path):
    store=Store(tmp_path/'history')
    job=store.enqueue('Prior job','sample',[dict(name='before',seconds=0)])
    store.db.execute('DROP TABLE workflow_links')
    store.db.execute('PRAGMA user_version=20')
    store.db.commit()
    store.close()
    store=Store(tmp_path/'history')
    try:
        assert execute_job(store,job)=='succeeded'
        from media_tool.jobs import SCHEMA
        assert store.db.execute('PRAGMA user_version').fetchone()[0]==SCHEMA
    finally:store.close()


def test_saved_draft_reopens_without_using_new_file_identities(window,tmp_path):
    from PySide6.QtCore import QSettings
    from media_tool.window import Workspace
    source=image(tmp_path/'source.png')
    window.model.append([source])
    window.open_jobs()
    editor=window.jobs_dialog.workflow_editor
    editor.new()
    editor.use_workspace()
    window._tool_changed('Images')
    editor.capture()
    editor.name.setText('Saved workflow')
    window.settings.sync()
    path=window.settings.fileName()
    window.close()
    wait_until(lambda:not window.busy)
    second=Workspace(QSettings(path,QSettings.Format.IniFormat))
    try:
        second.open_jobs()
        restored=second.jobs_dialog.workflow_editor
        assert restored.name.text()=='Saved workflow'
        assert restored.records()[0].identity_key==source.identity_key
        assert restored.records()[0].modified_ns==source.modified_ns
        assert len(restored.steps)==1
    finally:second.close()


@pytest.mark.parametrize('steps',[
    [step('images'),step('video')],
    [step('archives'),step('rename'),step('video')],
    [step('video'),step('archives',archive_format='cbz')],
    [step('documents',document_task='text'),step('audio')],
])
def test_known_incompatible_steps_rejected_before_start(tmp_path,steps):
    source=image(tmp_path/'source.png')
    with pytest.raises(ValueError,match='cannot use the preceding'):
        workflow_snapshot([source],steps)


def test_produced_paths_follow_rename_in_queue_details(window,tmp_path):
    source=image(tmp_path/'source.png')
    queue=window.ensure_jobs()
    job=enqueue(queue.store,[source],[step('images',extension='jpg'),step('rename',prefix='Final_'),step('archives')])
    assert execute_job(queue.store,job)=='succeeded'
    rename=json.loads(queue.store.items(job)[1]['stage'])
    assert rename['renames'][0]['after']==rename['outputs'][0]['source']
    queue.refresh(job)
    queue.items_table.selectRow(0)
    assert rename['outputs'][0]['source'] in queue.detail.text()


def test_tool_action_saves_step_without_touching_original(window,tmp_path):
    source=image(tmp_path/'source.png')
    window.model.append([source])
    window.open_jobs()
    editor=window.jobs_dialog.workflow_editor
    editor.new()
    editor.use_workspace()
    window._tool_changed('Images')
    assert window.start_rename_button.text()=='Add step'
    assert not window.keep.isVisible() and not window.collision.isVisible()
    assert not window.workflow_policy.isVisible()
    window.visual.groups['Advanced'].help.update_tip()
    assert window.workflow_policy.text() in window.visual.groups['Advanced'].help.toolTip()
    window.start_rename_button.click()
    assert len(editor.steps)==1
    assert window.jobs_dialog.store.jobs()==[]
    assert list(tmp_path.glob('*.jpg'))==[]
    window._tool_changed('Rename')
    window.start_rename_button.click()
    assert len(editor.steps)==2 and Path(source.path).exists()
    window._tool_changed('Images')
    editor.leave()
    assert not window.workflow_bar.isVisible()
    assert window.start_rename_button.text()!='Add step'
    assert window.keep.isVisible()
    window.visual.groups['Advanced'].header.setChecked(True)
    assert window.collision.isVisible()


def test_protected_pdf_chain_reenters_passwords_without_persisting(window,tmp_path,monkeypatch):
    from io import BytesIO
    from pypdf import PdfReader
    from PySide6.QtWidgets import QInputDialog
    from media_tool.private_values import remember,forget
    from test_documents import pdf
    old,new='Old 日本 pass','New café pass'
    source=pdf(tmp_path/'locked.pdf',1,old)
    input_token,output_token=remember(old),remember(new)
    request=step('documents',document_task='protect',document_protection='set',
        document_input_secret=input_token,document_output_secret=output_token)
    queue=window.ensure_jobs()
    job=enqueue(queue.store,[source],[request,step('rename',prefix='PDF_'),step('archives')])
    forget(input_token)
    forget(output_token)
    monkeypatch.setattr(QInputDialog,'getText',lambda parent,title,*a:(old if title.startswith('Input') else new,True))
    queue.start_single(job)
    wait_until(lambda:queue.worker is None and not queue.enabled,40000)
    assert queue.saved_jobs()[-1]['state']=='succeeded',queue.store.items(job)
    with zipfile.ZipFile(outputs(queue.store,job)[0]['source']) as archive:
        document=PdfReader(BytesIO(archive.read('PDF_0001.pdf')))
        assert document.is_encrypted and document.decrypt(new)
    stored=json.dumps(queue.store.items(job),ensure_ascii=False)
    assert old not in stored and new not in stored
    forget(input_token)
    forget(output_token)
