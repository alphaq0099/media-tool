from pathlib import Path
from dataclasses import replace
import hashlib
import json
import pytest
from pypdf import PdfWriter, PdfReader
import pypdfium2 as pdfium

from media_tool.domain import FileRecord
from media_tool.planning import PlanRequest
from media_tool.documents import plan_documents, document_snapshot, run_document, invoke, page_selection
from media_tool.jobs import Store,execute_job
from media_tool.private_values import remember
from test_foundation import app,window,wait_until


def pdf(path, count=3, password=""):
    writer = PdfWriter()
    for index in range(count):
        writer.add_blank_page(width=200+index*10,height=300)
    writer.add_metadata({"/Title":"Original 日本", "/Author":"Author"})
    writer.add_outline_item("First",0)
    if password:
        writer.encrypt(password,algorithm="AES-256")
    with path.open("wb") as handle:
        writer.write(handle)
    writer.close()
    return FileRecord.from_path(path)


def text_pdf(path, texts=("Hello café", "Second page")):
    from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject
    writer=PdfWriter()
    font=writer._add_object(DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),
        NameObject('/BaseFont'):NameObject('/Helvetica'),NameObject('/Encoding'):NameObject('/WinAnsiEncoding')}))
    for text in texts:
        page=writer.add_blank_page(595,842)
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
        stream=DecodedStreamObject()
        stream.set_data(b'BT /F1 16 Tf 40 780 Td <'+text.encode('cp1252').hex().encode()+b'> Tj ET')
        page[NameObject('/Contents')]=writer._add_object(stream)
    writer.write(path)
    writer.close()


def make(records, **options):
    return plan_documents(records,PlanRequest("documents",extension=options.pop("extension","pdf"),**options))


def execute(tmp_path,plan,hook=lambda point:None):
    store = Store(tmp_path/"history")
    try:
        job = store.enqueue("Documents", "documents", document_snapshot(plan))
        execute_job(store,job,hook=hook)
        return store.items(job)
    finally:
        store.close()


@pytest.mark.parametrize("task,pages,expected", [("extract","3,1",[220,200]),("reorder","3-1",[220,210,200]),("remove","2",[200,220]),("rotate","2",[200,300,220])])
def test_pdf_page_operations_and_geometry(tmp_path,task,pages,expected):
    source = pdf(tmp_path/"source 日本.pdf")
    before = Path(source.path).read_bytes()
    plan = make([source],document_task=task,document_pages=pages)
    assert plan.counts=={"Planned":1},plan.items
    rows = execute(tmp_path,plan)
    assert rows[0]["state"]=="succeeded",rows
    with pdfium.PdfDocument(plan.items[0].output) as result:
        assert [round(result[i].get_size()[0]) for i in range(len(result))]==expected
    assert Path(source.path).read_bytes()==before


def test_merge_order_and_split_ranges(tmp_path):
    first = pdf(tmp_path/"one.pdf",2)
    second = pdf(tmp_path/"two.pdf",1)
    plan = make([first,second],document_task="merge",document_order=(second.path,first.path))
    assert execute(tmp_path,plan)[0]["state"]=="succeeded"
    reader = PdfReader(plan.items[0].output)
    assert len(reader.pages)==3
    split = make([FileRecord.from_path(plan.items[0].output)],document_task="split",document_split_mode="ranges",document_ranges="1-2\n3")
    assert split.counts=={"Planned":2}
    assert all(r["state"]=="succeeded" for r in execute(tmp_path,split))
    assert [len(PdfReader(i.output).pages) for i in split.items]==[2,1]


def test_range_errors_and_forms_are_blocked(tmp_path):
    source = pdf(tmp_path/"source.pdf")
    assert make([source],document_pages="4").items[0].status=="Invalid"
    with pytest.raises(ValueError):
        page_selection("1,1",3)
    writer = PdfWriter()
    writer.add_blank_page(200,200)
    from pypdf.generic import DictionaryObject,NameObject,ArrayObject
    writer._root_object[NameObject("/AcroForm")] = DictionaryObject({NameObject("/Fields"):ArrayObject([DictionaryObject()])})
    path = tmp_path/"form.pdf"
    writer.write(path)
    blocked = make([FileRecord.from_path(path)])
    assert blocked.items[0].status=="Invalid" and "interactive" in blocked.items[0].reason


def test_encryption_unicode_metadata_and_removal(tmp_path):
    source = pdf(tmp_path/"locked.pdf",password="日本 secret")
    invalid = make([source],document_task="metadata")
    assert invalid.items[0].status=="Invalid"
    token = remember("日本 secret")
    output_token = remember("new café password")
    plan = make([source],document_task="metadata",document_input_secret=token,document_protection="set",document_output_secret=output_token,
                document_metadata_mode="edit",document_title="Updated 日本")
    assert execute(tmp_path,plan)[0]["state"]=="succeeded"
    reader = PdfReader(plan.items[0].output)
    assert reader.is_encrypted and not reader.decrypt("wrong")
    assert reader.decrypt("new café password") and reader.metadata.title=="Updated 日本"
    data = json.dumps(document_snapshot(plan))
    assert "日本 secret" not in data and "new café password" not in data
    stripped = make([source],document_task="protect",document_input_secret=token,document_protection="remove",document_metadata_mode="remove")
    rows = execute(tmp_path,stripped)
    assert rows[0]["state"]=="succeeded",rows
    result = PdfReader(stripped.items[0].output)
    assert not result.is_encrypted and not result.metadata


def test_images_pdf_natural_order_and_render(tmp_path):
    from PIL import Image
    inputs=[]
    for name,color in (("Page 10.png","blue"),("Page 2.png","red")):
        path=tmp_path/name
        Image.new("RGBA",(100,60),color).save(path)
        inputs.append(FileRecord.from_path(path))
    plan=make(inputs,document_task="images_pdf")
    assert [Path(s["source"]).name for s in plan.items[0].document_spec["inputs"]]==["Page 2.png","Page 10.png"]
    rows=execute(tmp_path,plan)
    assert rows[0]["state"]=="succeeded",rows
    with pdfium.PdfDocument(plan.items[0].output) as result:
        assert len(result)==2
        for index,expected in ((0,(255,0,0)),(1,(0,0,255))):
            bitmap=result[index].render(scale=1)
            image=bitmap.to_pil().convert("RGB")
            actual=image.getpixel((image.width//2,image.height//2))
            assert max(abs(a-b) for a,b in zip(actual,expected))<5
            bitmap.close()
    source=FileRecord.from_path(plan.items[0].output)
    rendered=make([source],document_task="render",extension="png",document_pages="2",document_dpi=100)
    rows=execute(tmp_path,rendered)
    assert rows[0]["state"]=="succeeded",rows
    with Image.open(rendered.items[0].output) as image:
        assert list(image.size)==rendered.items[0].document_spec["raster_size"]


def test_text_export_and_scanned_pdf_rejection(tmp_path):
    path=tmp_path/"text.pdf"
    text_pdf(path)
    plan=make([FileRecord.from_path(path)],document_task="text")
    rows=execute(tmp_path,plan)
    assert rows[0]["state"]=="succeeded",rows
    text=Path(plan.items[0].output).read_text(encoding="utf-8")
    assert "Hello café" in text and "Second page" in text and "\f" in text
    scanned=pdf(tmp_path/"blank.pdf")
    empty=make([scanned],document_task="text")
    rows=execute(tmp_path,empty)
    assert rows[0]["state"]=="failed" and "OCR" in rows[0]["message"]
    assert not Path(empty.items[0].output).exists()


def test_structural_optimization_and_metadata_removal(tmp_path):
    source=pdf(tmp_path/"source.pdf")
    plan=make([source],document_task="optimize",document_metadata_mode="remove")
    rows=execute(tmp_path,plan)
    assert rows[0]["state"]=="succeeded",rows
    reader=PdfReader(plan.items[0].output)
    assert not reader.metadata and len(reader.pages)==3
    assert len(reader.outline)==1


def test_late_collision_and_modified_original(tmp_path):
    source=pdf(tmp_path/"source.pdf")
    plan=make([source],document_task="extract",document_pages="1")
    output=Path(plan.items[0].output)
    output.write_bytes(b"unrelated")
    rows=execute(tmp_path,plan)
    assert rows[0]["state"]=="failed" and output.read_bytes()==b"unrelated"
    plan=make([source],document_task="extract",document_pages="1")
    with Path(source.path).open("ab") as handle:handle.write(b"changed")
    assert execute(tmp_path,plan)[0]["state"]=="failed"
    assert not Path(plan.items[0].output).exists()


def test_document_ui_lazy_shared_start_and_saved_results(window,tmp_path):
    from test_foundation import wait_until
    assert window.document_controls is None
    first=pdf(tmp_path/"first.pdf",1)
    second=pdf(tmp_path/"second.pdf",2)
    window._tool_changed("Documents")
    window.model.append([first,second])
    assert window.document_controls.panel.isVisible() and not window.audio_controls.panel.isVisible()
    queue=window.ensure_jobs()
    window.start_rename_button.click()
    wait_until(lambda:window.plan_worker is None and queue.worker is None and window.tabs.currentIndex()==2,40000)
    assert window.result_model.items[0]["state"]=="succeeded",window.status.text()
    assert not window.result_can_undo and window.model.rowCount()==0
    queue.refresh(window.last_result_job)
    assert queue.archive_results_button.text()=="Document results"
    queue.show_archive_results()
    assert window.tool=="Documents"


def test_password_confirmation_error_stays_in_ui(window,tmp_path):
    window._tool_changed("Documents")
    source=pdf(tmp_path/"source.pdf")
    window.model.append([source])
    controls=window.document_controls
    controls.task.setCurrentIndex(controls.task.findData("protect"))
    controls.protection.setCurrentIndex(controls.protection.findData("set"))
    controls.output_password.setText("new password")
    controls.confirm.setText("wrong")
    assert window.preview_changes() is False
    assert "do not match" in window.status.text()


def test_reordered_text_pages_render_identically(tmp_path):
    path=tmp_path/'content.pdf'
    text_pdf(path,('Page one','Page two','Page three'))
    plan=make([FileRecord.from_path(path)],document_task='reorder',document_pages='3,1,2')
    assert execute(tmp_path,plan)[0]['state']=='succeeded'
    def hashes(path):
        result=[]
        with pdfium.PdfDocument(str(path)) as document:
            for index in range(len(document)):
                page=document[index]
                bitmap=page.render(scale=1)
                result.append(hashlib.sha256(bitmap.to_pil().tobytes()).hexdigest())
                bitmap.close()
                page.close()
        return result
    original=hashes(path)
    assert hashes(plan.items[0].output)==[original[2],original[0],original[1]]


def test_image_downsampling_and_dpi_budget(tmp_path):
    from PIL import Image
    path=tmp_path/'large.png'
    Image.new('RGB',(1200,800),'#28a8c8').save(path)
    initial=make([FileRecord.from_path(path)],document_task='images_pdf')
    assert execute(tmp_path,initial)[0]['state']=='succeeded'
    source=FileRecord.from_path(initial.items[0].output)
    plan=make([source],document_task='optimize',document_downsample=True,document_image_dpi=36)
    rows=execute(tmp_path,plan)
    assert rows[0]['state']=='succeeded',rows
    original=next(iter(PdfReader(source.path).pages[0].images)).image
    optimized=next(iter(PdfReader(plan.items[0].output).pages[0].images)).image
    assert optimized.width<original.width and optimized.height<original.height
    excessive=make([source],document_task='render',extension='png',document_dpi=600)
    assert excessive.items[0].status=='Invalid' and 'megapixel' in excessive.items[0].reason


def test_all_document_tasks_switch_without_stale_formats(window):
    window._tool_changed('Documents')
    controls=window.document_controls
    for index in range(controls.task.count()):
        controls.task.setCurrentIndex(index)
        task=controls.task.currentData()
        assert window.plan_request().document_task==task
        assert window.format.currentText()==('PNG' if task=='render' else 'TXT' if task=='text' else 'Searchable PDF' if task=='ocr' else 'PDF')
    window._tool_changed('Audio')
    assert not controls.panel.isVisible()
    window._tool_changed('Documents')
    assert controls.panel.isVisible()
