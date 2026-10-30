from pathlib import Path
import hashlib
import pytest
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import RectangleObject
import pypdfium2 as pdfium

from media_tool.domain import FileRecord
from media_tool.documents import invoke,document_snapshot
from media_tool.jobs import Store,execute_job
from media_tool.private_values import remember
from test_documents import make,execute
from test_foundation import app,window,wait_until


def fractional(path,rotation=0,crop=False,password=''):
    writer=PdfWriter()
    page=writer.add_blank_page(595.2,841.92)
    if crop:page.cropbox=RectangleObject((-12.3,-4.4,610.7,850.6))
    if rotation:page.rotate(rotation)
    if password:writer.encrypt(password,algorithm='AES-256')
    writer.write(path)
    writer.close()
    return FileRecord.from_path(path)


@pytest.mark.parametrize('extension',['jpg','png'])
@pytest.mark.parametrize('rotation',[0,90])
@pytest.mark.parametrize('dpi',[150,300])
def test_fractional_word_geometry_renders_and_verifies_exactly(tmp_path,extension,rotation,dpi):
    source=fractional(tmp_path/'Word-style A4.pdf',rotation)
    before=hashlib.sha256(Path(source.path).read_bytes()).hexdigest()
    plan=make([source],document_task='render',extension=extension,document_dpi=dpi,document_quality=100)
    assert plan.counts=={'Planned':1},plan.items
    spec=plan.items[0].document_spec
    result=execute(tmp_path,plan)
    assert result[0]['state']=='succeeded',result
    with pdfium.PdfDocument(source.path) as pdf:
        page=pdf[0]
        bitmap=page.render(scale=dpi/72)
        expected=(bitmap.width,bitmap.height)
        bitmap.close();page.close()
    with Image.open(plan.items[0].output) as image:
        assert image.size==expected==tuple(spec['raster_size'])
        assert image.format==('JPEG' if extension=='jpg' else 'PNG')
    assert hashlib.sha256(Path(source.path).read_bytes()).hexdigest()==before


@pytest.mark.parametrize('rotation',[0,270])
def test_visible_crop_geometry_and_encrypted_pdf(tmp_path,rotation):
    source=fractional(tmp_path/'locked crop.pdf',rotation,crop=True,password='render secret')
    plan=make([source],document_task='render',extension='jpg',document_dpi=150,
        document_input_secret=remember('render secret'))
    assert execute(tmp_path,plan)[0]['state']=='succeeded'
    with Image.open(plan.items[0].output) as image:
        assert list(image.size)==plan.items[0].document_spec['raster_size']


def test_legacy_saved_render_job_can_retry_without_recreating_it(tmp_path):
    source=fractional(tmp_path/'Word.pdf')
    plan=make([source],document_task='render',extension='jpg',document_dpi=150)
    specs=document_snapshot(plan)
    spec=dict(specs[0]);spec.pop('raster_geometry')
    spec['raster_size']=[1240,1754]
    store=Store(tmp_path/'history')
    try:
        job=store.enqueue('Existing failed render','documents',[spec])
        item=store.items(job)[0]
        store.set_item(item['id'],'failed','Old dimension verification failed.')
        store.summarize(job)
        store.retry(job)
        assert execute_job(store,job)=='succeeded',store.items(job)
        with Image.open(spec['output']) as image:assert image.size==(1241,1754)
    finally:store.close()


def test_wrong_dimensions_and_wrong_format_are_still_rejected(tmp_path):
    source=fractional(tmp_path/'Word.pdf')
    plan=make([source],document_task='render',extension='jpg',document_dpi=150)
    spec=plan.items[0].document_spec
    wrong=tmp_path/'wrong.jpg'
    Image.new('RGB',(1240,1754)).save(wrong)
    with pytest.raises(Exception,match='verification failed'):
        invoke(dict(mode='verify',spec=spec,output=str(wrong),secrets={}))
    Image.new('RGB',tuple(spec['raster_size'])).save(wrong,format='PNG')
    with pytest.raises(Exception,match='verification failed'):
        invoke(dict(mode='verify',spec=spec,output=str(wrong),secrets={}))


def test_document_render_ui_succeeds_at_reported_settings(window,tmp_path):
    source=fractional(tmp_path/'Word-style final.pdf')
    window._tool_changed('Documents')
    controls=window.document_controls
    controls.task.setCurrentIndex(controls.task.findData('render'))
    controls.dpi.setValue(150)
    controls.quality.setValue(100)
    window.format.setCurrentText('JPEG')
    window.model.append([source])
    queue=window.ensure_jobs()
    window.start_rename_button.click()
    wait_until(lambda:window.plan_worker is None and queue.worker is None and window.tabs.currentIndex()==2,30000)
    assert window.result_model.items[0]['state']=='succeeded',window.result_model.items


@pytest.mark.parametrize('paper',['a4','letter','image'])
def test_images_to_pdf_paper_geometry_is_independent_of_integer_types(tmp_path,paper):
    from media_tool.documents import sizes_digest
    assert sizes_digest([[612,792]])==sizes_digest([[612.0,792.0]])
    path=tmp_path/'image.png'
    Image.new('RGB',(60,40),'teal').save(path)
    plan=make([FileRecord.from_path(path)],document_task='images_pdf',document_paper=paper,document_margin=5)
    result=execute(tmp_path,plan)
    assert result[0]['state']=='succeeded',result
