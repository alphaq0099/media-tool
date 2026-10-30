from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import zipfile

import pytest
from PIL import Image

from media_tool.domain import FileRecord
from media_tool.planning import PlanRequest
from media_tool.jobs import Store,execute_job
from media_tool.workflows import workflow_snapshot
from test_foundation import app,window,wait_until


def step(operation,**settings):
    return dict(label=operation.title(),request=asdict(PlanRequest(operation,**settings)))


def image(path):
    Image.new('RGB',(64,48),'green').save(path)
    return FileRecord.from_path(path)


def enqueue(store,records,steps):
    return store.enqueue('Test workflow','workflow',workflow_snapshot(records,steps))


def outputs(store,job):
    return json.loads(store.items(job)[-1]['stage'])['outputs']


def test_actual_image_rename_archive_and_originals(tmp_path):
    source=image(tmp_path/'source.png')
    before=Path(source.path).read_bytes()
    store=Store(tmp_path/'history')
    try:
        job=enqueue(store,[source],[step('images',extension='jpg'),step('rename',prefix='Holiday_',padding=2),
            step('archives',archive_format='zip',archive_name='Holiday')])
        assert execute_job(store,job)=='succeeded',store.items(job)
        rows=store.items(job)
        assert all(r['state']=='succeeded' for r in rows)
        renamed=json.loads(rows[1]['stage'])['outputs'][0]['source']
        with zipfile.ZipFile(outputs(store,job)[0]['source']) as archive:
            assert archive.namelist()==['Holiday_01.jpg']
            assert archive.read('Holiday_01.jpg')==Path(renamed).read_bytes()
        assert Path(source.path).read_bytes()==before
        assert len(store.visible_jobs())==1 and len(store.jobs())==4
    finally:store.close()


def test_mkv_to_mp4_rename_7z(tmp_path):
    from test_video import clip
    from media_tool.processes import run_process
    from media_tool.tool_paths import resolve_tool
    source=clip(tmp_path/'video 日本.mkv',audio=1,duration=1)
    before=Path(source.path).read_bytes()
    store=Store(tmp_path/'history')
    try:
        job=enqueue(store,[source],[step('video',extension='mp4'),step('rename',prefix='Movie_',padding=2),
            step('archives',archive_format='7z',archive_name='Movie')])
        assert execute_job(store,job)=='succeeded',store.items(job)
        archive=outputs(store,job)[0]['source']
        listing=run_process([resolve_tool('7z'),'l','-slt',archive])
        assert 'Movie_01.mp4' in listing
        assert Path(source.path).read_bytes()==before
        assert (tmp_path/'Movie_01.mp4').is_file()
    finally:store.close()


def test_failed_step_blocks_all_dependents(tmp_path):
    source=image(tmp_path/'image.png')
    store=Store(tmp_path/'history')
    try:
        job=enqueue(store,[source],[step('video',extension='mp4'),step('rename'),step('archives')])
        execute_job(store,job)
        rows=store.items(job)
        assert [r['state'] for r in rows]==['failed','blocked','blocked']
        assert store.visible_jobs()[0]['state']=='failed'
        assert len(store.jobs())==1
    finally:store.close()


@pytest.mark.parametrize('point,occurrence',[('workflow_planned',1),('workflow_child_finished',1),('workflow_child_finished',2),('workflow_checkpoint',1),('workflow_checkpoint',2),('committed',2)])
def test_crash_retry_preserves_completed_steps(tmp_path,point,occurrence):
    source=image(tmp_path/'original.png')
    history=tmp_path/'history'
    store=Store(history)
    class Crash(BaseException):pass
    count=0
    def hook(stage):
        nonlocal count
        if stage==point:
            count+=1
            if count==occurrence:raise Crash()
    try:
        job=enqueue(store,[source],[step('images',extension='jpg'),step('rename',prefix='Final_'),step('archives')])
        with pytest.raises(Crash):execute_job(store,job,hook=hook)
    finally:store.close()
    store=Store(history)
    try:
        store.recover()
        store.retry(job)
        assert execute_job(store,job)=='succeeded',store.items(job)
        assert len(store.jobs())==4
        first=json.loads(store.items(job)[0]['stage'])['child_job']
        assert store.items(first)[0]['attempts']<=2
        assert len(list(tmp_path.glob('Final_*.jpg')))==1
        assert len(list(tmp_path.glob('Archive*.zip')))==1
        assert Path(source.path).is_file()
    finally:store.close()


def test_rename_must_not_be_first_and_changed_inputs_are_rejected(tmp_path):
    source=image(tmp_path/'original.png')
    with pytest.raises(ValueError,match='Start with'):
        workflow_snapshot([source],[step('rename')])
    store=Store(tmp_path/'history')
    try:
        job=enqueue(store,[source],[step('images'),step('archives')])
        Path(source.path).write_bytes(b'changed')
        execute_job(store,job)
        assert [r['state'] for r in store.items(job)]==['failed','blocked']
    finally:store.close()


def test_embedded_queue_and_workflow_ui(window,tmp_path):
    source=image(tmp_path/'source.png')
    window.model.append([source])
    window.open_jobs()
    queue=window.jobs_dialog
    assert window.workspace_stack.currentWidget() is queue
    assert not queue.isWindow() and queue.window() is window
    assert window.jobs_button.isChecked()
    editor=queue.workflow_editor
    editor.new()
    editor.use_workspace()
    window._tool_changed('Images')
    window.format.setCurrentText('JPEG')
    window.workflow_capture_button.click()
    assert len(editor.steps)==1
    window._tool_changed('Rename')
    window.prefix.setText('Holiday_')
    window.workflow_capture_button.click()
    window._tool_changed('Archives')
    window.format.setCurrentText('ZIP')
    window.workflow_capture_button.click()
    assert [s['request']['operation'] for s in editor.steps]==['images','rename','archives']
    assert window.workspace_stack.currentWidget() is queue
    editor.submit(True)
    wait_until(lambda:queue.worker is None and queue.plan_worker is None and not queue.enabled,50000)
    assert queue.saved_jobs()[-1]['state']=='succeeded',queue.status.text()
    assert queue.jobs_model.rowCount()==1
    assert queue.views.model.rowCount()==0
    queue.tabs.setCurrentWidget(queue.views.history)
    queue.items_table.selectRow(2)
    assert queue.child_model.rowCount()==1
    assert queue.child_table.isVisible()


def test_edit_recipe_draft_and_passwords_not_persisted(window,tmp_path):
    window.open_jobs()
    editor=window.jobs_dialog.workflow_editor
    editor.new()
    window._tool_changed('Archives')
    window.format.setCurrentText('7z')
    window.archive_controls.password.setText('Secret 日本')
    window.archive_controls.confirm_password.setText('Secret 日本')
    editor.capture()
    assert len(editor.steps)==1
    editor.name.setText('Archive recipe')
    editor.save_recipe()
    data=str(window.settings.value('workflow/recipes'))+str(window.settings.value('workflow/draft'))
    assert 'Secret 日本' not in data
    editor.load_recipe()
    assert editor.steps[0]['archive_secret']
    assert editor.steps[0]['request']['archive_encrypt']
    editor.step_list.setCurrentRow(0)
    editor.edit()
    assert window.tool=='Archives' and window.format.currentText()=='7z'


@pytest.mark.parametrize('mode',['pause','cancel','tamper'])
def test_handoff_controls_and_changed_output(tmp_path,mode):
    source=image(tmp_path/'source.png')
    store=Store(tmp_path/'history')
    stopped=False
    def hook(point):
        nonlocal stopped
        if point=='workflow_checkpoint' and not stopped:
            stopped=True
            if mode=='tamper':
                first=json.loads(store.items(job)[0]['stage'])['outputs'][0]['source']
                Path(first).write_bytes(b'changed output')
    try:
        job=enqueue(store,[source],[step('images',extension='jpg'),step('rename'),step('archives')])
        execute_job(store,job,cancelled=lambda:stopped and mode=='cancel',paused=lambda:stopped and mode=='pause',hook=hook)
        rows=store.items(job)
        assert rows[0]['state']=='succeeded'
        assert rows[1]['state']==dict(pause='queued',cancel='cancelled',tamper='failed')[mode]
        first=json.loads(rows[0]['stage'])['child_job']
        before=store.items(first)[0]['attempts']
        if mode=='tamper':
            assert rows[2]['state']=='blocked'
            assert not list(tmp_path.glob('Archive*.zip'))
        else:
            if mode=='cancel':store.retry(job)
            assert execute_job(store,job)=='succeeded',store.items(job)
            assert store.items(first)[0]['attempts']==before
    finally:store.close()


def test_actual_split_outputs_feed_combined_archive(tmp_path):
    from test_audio_edit import fixture
    source=fixture(tmp_path/'source.wav',duration=1)
    store=Store(tmp_path/'history')
    try:
        job=enqueue(store,[source],[step('audio',extension='wav',audio_action='split',audio_ranges='0 - 0.5\n0.5 - end'),
            step('rename',prefix='Part_',padding=1),step('archives')])
        assert execute_job(store,job)=='succeeded',store.items(job)
        with zipfile.ZipFile(outputs(store,job)[0]['source']) as archive:
            assert archive.namelist()==['Part_1.wav','Part_2.wav']
    finally:store.close()


def test_documents_workflow_and_unchanged_rename(tmp_path):
    source=image(tmp_path/'Page.png')
    store=Store(tmp_path/'history')
    try:
        job=enqueue(store,[source],[step('documents',document_task='images_pdf',extension='pdf'),
            step('rename',rename_style='clean'),step('archives')])
        assert execute_job(store,job)=='succeeded',store.items(job)
        assert outputs(store,job)[0]['source'].endswith('.zip')
    finally:store.close()


def test_unique_collisions_flow_to_archive(tmp_path):
    source=image(tmp_path/'source.png')
    unrelated=tmp_path/'Final_0001.jpg'
    unrelated.write_bytes(b'unrelated')
    store=Store(tmp_path/'history')
    try:
        job=enqueue(store,[source],[step('images',extension='jpg'),step('rename',prefix='Final_'),step('archives')])
        assert execute_job(store,job)=='succeeded',store.items(job)
        assert unrelated.read_bytes()==b'unrelated'
        name=Path(json.loads(store.items(job)[1]['stage'])['outputs'][0]['source']).name
        assert name!='Final_0001.jpg'
        with zipfile.ZipFile(outputs(store,job)[0]['source']) as archive:assert archive.namelist()==[name]
    finally:store.close()


def test_edit_video_step_restores_settings(window):
    window.open_jobs()
    editor=window.jobs_dialog.workflow_editor
    editor.new()
    window._tool_changed('Video')
    window.format.setCurrentText('MKV')
    window.video_format_controls.codec.setCurrentIndex(window.video_format_controls.codec.findData('hevc'))
    window.video_speed.setCurrentIndex(0)
    expected=window.plan_request()
    editor.capture()
    window._tool_changed('Video')
    window.format.setCurrentText('MP4')
    window.video_format_controls.codec.setCurrentIndex(window.video_format_controls.codec.findData('h264'))
    editor.step_list.setCurrentRow(0)
    editor.edit()
    actual=window.plan_request()
    assert (actual.extension,actual.video_codec,actual.video_preset)==(expected.extension,expected.video_codec,expected.video_preset)


def test_navigation_during_workflow_and_queue_never_opens_window(window,tmp_path):
    source=image(tmp_path/'source.png')
    window.open_jobs()
    queue=window.jobs_dialog
    job=enqueue(queue.store,[source],[step('images',extension='jpg'),step('rename'),step('archives')])
    queue.start_single(job)
    window._tool_changed('Video')
    assert not window.start_rename_button.isEnabled()
    assert window.jobs_button.isEnabled()
    window.jobs_button.click()
    assert window.workspace_stack.currentWidget() is queue
    assert not queue.isWindow()
    wait_until(lambda:queue.worker is None and not queue.enabled,40000)
    assert queue.saved_jobs()[-1]['state']=='succeeded'
