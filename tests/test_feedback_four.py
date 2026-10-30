"""Screenshot feedback, workflow ordering, PDF compression and background intake."""
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
from PIL import Image
from pypdf import PdfReader,PdfWriter
from PySide6.QtTest import QTest

from test_foundation import app,window,wait_until
from test_documents import pdf,text_pdf,make,execute
from test_workflows import image,step,enqueue,outputs
from media_tool.jobs import Store,execute_job
from media_tool.output_paths import resolved_output,job_output_paths,open_output_path


def test_inspector_does_not_fill_dead_space_after_mode_changes(window):
    window.resize(1180,1100)
    window._tool_changed('Video')
    c=window.compression_controls
    for mode in ('target_size','quality','target_size','quality'):
        c.mode.setCurrentIndex(c.mode.findData(mode))
        wait_until(lambda: window.visual.groups['Video'].height() <= window.visual.groups['Video'].sizeHint().height()+2)
        group=window.visual.groups['Video']
        assert group.height()<=group.sizeHint().height()+2
        assert group.body.height()<=group.body.sizeHint().height()+2
        assert window.visual.groups['Tracks'].y()-group.geometry().bottom()<=12


def test_workflow_tools_follow_sidebar_order(window):
    q=window.ensure_jobs()
    combo=q.workflow_editor.tool
    assert [combo.itemText(i) for i in range(combo.count())]==['Images','Video','Audio','Rename','Archives','Documents']


def test_image_rename_pdf_ignores_old_document_order_and_resolves_renames(tmp_path):
    source=image(tmp_path/'input.png')
    before=Path(source.path).read_bytes()
    store=Store(tmp_path/'history')
    try:
        job=enqueue(store,[source],[step('images',extension='jpg',resize_mode='percent',resize_percent=150,image_quality=100),
            step('rename',prefix='51',padding=1),step('documents',extension='pdf',document_task='images_pdf',
                document_order=(str(tmp_path/'unrelated1.pdf'),str(tmp_path/'unrelated2.pdf')))])
        assert execute_job(store,job)=='succeeded',store.items(job)
        rows=store.items(job)
        result=outputs(store,job)[0]['source']
        assert len(PdfReader(result).pages)==1
        first=json.loads(rows[0]['stage'])
        renamed=json.loads(rows[1]['stage'])['outputs'][0]['source']
        old=first['outputs'][0]['source']
        assert not Path(old).exists()
        assert resolved_output(store,first['child_job'],old)==renamed
        assert job_output_paths(store,job)[0]==result
        assert Path(source.path).read_bytes()==before
    finally:store.close()


def test_rename_filter_explains_skipped_files(window,tmp_path):
    source=image(tmp_path/'photo.png')
    window._tool_changed('Rename')
    window.model.append([source])
    window.format.setCurrentText('Random names')
    window.rename_pattern.setText('jkihg')
    assert window.preview_changes()
    wait_until(lambda:window.plan_worker is None)
    assert window.last_plan.items[0].status=='Skipped'
    assert 'does not match' in window.preview_reason.text()
    assert window.pattern_label.text()=='Only names matching'
    window.rename_pattern.clear()
    assert window.preview_changes()
    wait_until(lambda:window.plan_worker is None)
    assert window.last_plan.items[0].status=='Planned'


def test_pdf_jpeg_compression_reduces_images_and_preserves_text(tmp_path):
    source=tmp_path/'scan.png'
    noise=Image.effect_noise((640,480),90).convert('RGB')
    noise.save(source);noise.close()
    from media_tool.domain import FileRecord
    image_plan=make([FileRecord.from_path(source)],document_task='images_pdf',document_quality=100)
    assert execute(tmp_path,image_plan)[0]['state']=='succeeded'
    text=tmp_path/'text.pdf'
    text_pdf(text,('Text must remain selectable',))
    combined=tmp_path/'combined.pdf'
    writer=PdfWriter()
    writer.append(image_plan.items[0].output);writer.append(text)
    writer.write(combined);writer.close()
    before=combined.read_bytes()
    plan=make([FileRecord.from_path(combined)],document_task='optimize',document_compression='jpeg',document_quality=45)
    rows=execute(tmp_path,plan)
    assert rows[0]['state']=='succeeded',rows
    output=Path(plan.items[0].output)
    assert output.stat().st_size<len(before)*.8
    reader=PdfReader(output)
    assert len(reader.pages)==2
    assert 'Text must remain selectable' in reader.pages[1].extract_text()
    assert next(iter(reader.pages[0].images)).image.size==(640,480)
    assert combined.read_bytes()==before


def test_pdf_compression_controls_and_frozen_request(window):
    window._tool_changed('Documents')
    c=window.document_controls
    c.task.setCurrentIndex(c.task.findData('optimize'))
    assert c.compression.isVisible()
    assert not c.quality.isVisible()
    c.compression.setCurrentIndex(c.compression.findData('jpeg'))
    assert c.quality.isVisible()
    assert window.plan_request().document_compression=='jpeg'
    window._tool_changed('Video');window._tool_changed('Documents')
    assert c.compression.currentData()=='jpeg'


def test_switching_document_task_clears_unrelated_order(window):
    window._tool_changed('Documents')
    c=window.document_controls
    c.task.setCurrentIndex(c.task.findData('merge'))
    c.join_order=('old1.pdf','old2.pdf')
    c.task.setCurrentIndex(c.task.findData('images_pdf'))
    assert c.join_order==()
    c.join_order=('picture2.png','picture1.png')
    c.update_visibility()
    assert c.join_order==('picture2.png','picture1.png')


def test_open_output_without_selection_and_association_fallback(window,tmp_path,monkeypatch):
    from media_tool.output_paths import QDesktopServices
    path=tmp_path/'complete.pdf';path.write_bytes(b'fixture')
    called=[]
    def open_url(url):
        called.append(url.toLocalFile())
        return len(called)>1
    monkeypatch.setattr(QDesktopServices,'openUrl',open_url)
    window.result_model.set_items((dict(source='source.pdf',output=str(path),state='succeeded',details='done'),))
    assert window.open_video_output()
    assert list(map(Path,called))==[path,tmp_path]
    assert 'Opened its folder' in window.status.text()


def test_missing_output_reports_the_problem(tmp_path,monkeypatch):
    from media_tool.output_paths import QDesktopServices
    called=[];messages=[]
    monkeypatch.setattr(QDesktopServices,'openUrl',lambda url:called.append(url.toLocalFile()) or True)
    assert open_output_path(tmp_path/'removed.pdf',messages.append)
    assert list(map(Path,called))==[tmp_path]
    assert 'moved or been removed' in messages[0]


def hold_job(monkeypatch,module,name):
    started,release=threading.Event(),threading.Event()
    original=getattr(module,name)
    def held(*args,**kwargs):
        started.set()
        if not release.wait(30):raise RuntimeError('Background test timed out')
        return original(*args,**kwargs)
    monkeypatch.setattr(module,name,held)
    return started,release


def test_can_prepare_and_queue_pdf_while_video_runs(window,tmp_path,monkeypatch):
    from media_tool import video
    from test_video import clip
    started,release=hold_job(monkeypatch,video,'run_video')
    source=clip(tmp_path/'movie.mkv',audio=1,duration=1)
    original=Path(source.path).read_bytes()
    window._tool_changed('Video')
    window.model.append([source]);window.update_counts()
    window.start_rename()
    try:
        wait_until(started.is_set,15000)
        q=window.jobs_dialog;worker=q.worker
        assert window.busy and not window.editing_busy
        window._tool_changed('Documents')
        document=pdf(tmp_path/'document.pdf',1)
        assert window.add_button.isEnabled()
        assert all(b.isEnabled() for b in window.nav_group.buttons())
        assert window.start_scan([document.path])
        wait_until(lambda:not window.editing_busy)
        assert len(window.model.items)==2
        assert window.proxy.rowCount()==1
        assert window.proxy.index(0,0).data()==Path(document.path).name
        assert window.preview_changes()
        wait_until(lambda:window.plan_worker is None,10000)
        assert window.last_plan.items[0].status=='Planned'
        assert window.run_button.isEnabled()
        assert not window.start_rename_button.isEnabled()
        window.convert_images(queued=True)
        pending=[j for j in q.saved_jobs() if j['adapter']=='documents']
        assert len(pending)==1 and pending[0]['state']=='queued'
        assert q.worker is worker and not worker.cancelled.is_set()
        assert window.tool=='Documents'
    finally:release.set()
    wait_until(lambda:window.jobs_dialog.worker is None,20000)
    window.jobs_dialog.start_queue()
    wait_until(lambda:window.jobs_dialog.worker is None and not window.jobs_dialog.enabled,20000)
    assert all(j['state']=='succeeded' for j in window.jobs_dialog.saved_jobs())
    assert Path(source.path).read_bytes()==original


def test_cancel_intake_does_not_cancel_background_job(window,tmp_path,monkeypatch):
    from test_visual_revamp import hold_conversion,start_image
    started,release=hold_conversion(monkeypatch)
    source,q=start_image(window,tmp_path)
    try:
        wait_until(started.is_set)
        cancelled=threading.Event()
        window.worker=SimpleNamespace(requestInterruption=cancelled.set)
        window.update_controls()
        assert not next(b for b in window.nav_group.buttons() if b.text()=='Video').isEnabled()
        window.cancel_scan()
        assert cancelled.is_set() and not q.worker.cancelled.is_set()
        cancelled.clear()
        window.cancel_scan(background=True)
        assert q.worker.cancelled.is_set() and not cancelled.is_set()
    finally:
        window.worker=None
        release.set()
    wait_until(lambda:q.worker is None,10000)
