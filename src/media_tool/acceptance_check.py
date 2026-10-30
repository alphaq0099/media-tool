"""Opt-in acceptance checks using generated files and isolated job history."""
from collections import Counter
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
import zipfile


def run(report):
    from PIL import Image, ImageDraw, ImageFont
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    import pypdfium2 as pdfium
    from . import __version__
    from .documents import plan_documents, document_snapshot
    from .domain import FileRecord
    from .jobs import Store, execute_job, Cancelled
    from .planning import PlanRequest
    from .private_values import remember
    from .document_external import available_tools
    from .workflows import workflow_snapshot
    from .processes import run_process
    from .tool_paths import resolve_tool

    report=report.resolve()
    report.parent.mkdir(parents=True,exist_ok=True)
    cases=[]
    def case(name, action, unavailable=None):
        if unavailable:
            cases.append(dict(name=name,status='unavailable',reason=unavailable))
            return
        started=time.monotonic()
        try:
            action()
            cases.append(dict(name=name,status='passed',seconds=round(time.monotonic()-started,3)))
        except Exception as error:
            cases.append(dict(name=name,status='failed',error=(type(error).__name__+': '+str(error))[:2000],seconds=round(time.monotonic()-started,3)))
        if sys.stdout:print(json.dumps(dict(case=name,status=cases[-1]['status'])),flush=True)

    with tempfile.TemporaryDirectory(prefix='mediatool-acceptance-',dir=report.parent) as scratch:
        root=Path(scratch)
        source=root/'Pages.pdf'
        writer=PdfWriter()
        font=writer._add_object(DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')}))
        for index in range(4):
            page=writer.add_blank_page(300+index*10,400)
            page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
            stream=DecodedStreamObject()
            stream.set_data(f'BT /F1 20 Tf 30 350 Td (Acceptance page {index+1}) Tj ET'.encode())
            page[NameObject('/Contents')]=writer._add_object(stream)
        writer.add_metadata({'/Title':'Original fixture'})
        writer.write(source)
        writer.close()
        scan=root/'scan.png'
        image=Image.new('RGB',(1600,300),'white')
        ImageDraw.Draw(image).text((60,100),'Media Tool acceptance 12345',font=ImageFont.load_default(size=48),fill='black')
        image.save(scan,dpi=(150,150));image.close()
        office=root/'document.odt'
        with zipfile.ZipFile(office,'w') as z:
            z.writestr('mimetype','application/vnd.oasis.opendocument.text')
            z.writestr('content.xml','''<?xml version="1.0"?><office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" office:version="1.2"><office:body><office:text><text:p>Media Tool acceptance 12345</text:p></office:text></office:body></office:document-content>''')
            z.writestr('META-INF/manifest.xml','''<?xml version="1.0"?><manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"><manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.text"/><manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/></manifest:manifest>''')
        originals={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in (source,scan,office)}
        counter=0
        def doc(task, inputs=None, hook=lambda _:None, **options):
            nonlocal counter
            counter+=1
            output=root/f'operation-{counter}'
            output.mkdir()
            plan=plan_documents([FileRecord.from_path(p) for p in (inputs or [source])],PlanRequest('documents',destination=str(output),document_task=task,extension=options.pop('extension','pdf'),**options))
            invalid=[item.reason for item in plan.items if item.status not in ('Planned','Skipped')]
            if invalid:raise ValueError('; '.join(invalid))
            store=Store(output/'history')
            try:
                job=store.enqueue(task,'documents',document_snapshot(plan))
                execute_job(store,job,hook=hook)
                rows=store.items(job)
                assert all(row['state']=='succeeded' for row in rows),[row['message'] for row in rows]
                assert not list(store.directory.glob('.mediatool-doc-*'))
            finally:store.close()
            return [Path(item.output) for item in plan.items]
        def widths(paths):
            return [int(page.mediabox.width) for p in paths for page in PdfReader(p).pages]
        def expect(actual, expected):
            assert actual==expected,(actual,expected)
        def fingerprint(path):
            result=[]
            with pdfium.PdfDocument(str(path)) as pdf:
                for page in pdf:
                    bitmap=page.render(scale=.5)
                    try:
                        image=bitmap.to_pil()
                        result.append((image.size,hashlib.sha256(image.tobytes()).hexdigest()))
                    finally:bitmap.close();page.close()
            return result
        def images_pdf():
            output=doc('images_pdf',[scan])[0]
            expect(len(PdfReader(output).pages),1)
        case('images-to-pdf',images_pdf)
        source_copy=root/'Other.pdf';source_copy.write_bytes(source.read_bytes())
        case('merge-order',lambda:expect(widths(doc('merge',[source,source_copy],document_order=(str(source),str(source_copy)))),[300,310,320,330]*2))
        case('split-pages',lambda:expect(widths(doc('split')),[300,310,320,330]))
        case('split-ranges',lambda:expect(widths(doc('split',document_split_mode='ranges',document_ranges='1-2\n3-4')),[300,310,320,330]))
        case('extract-order',lambda:expect(widths(doc('extract',document_pages='3,1')),[320,300]))
        case('reorder',lambda:expect(widths(doc('reorder',document_pages='4-1')),[330,320,310,300]))
        case('remove-pages',lambda:expect(widths(doc('remove',document_pages='2,4')),[300,320]))
        def rotate():
            output=doc('rotate',document_pages='2,4',document_rotation=90)[0]
            expect([p.rotation for p in PdfReader(output).pages],[0,90,0,90])
        case('rotate-pages',rotate)
        for extension in ('png','jpg'):
            def render(extension=extension):
                output=doc('render',extension=extension,document_pages='1',document_dpi=150)[0]
                with Image.open(output) as image:
                    image.load()
                    expect(image.size,(625,834))
                    expect(image.format,'PNG' if extension=='png' else 'JPEG')
            case('render-'+extension,render)
        def text():
            output=doc('text',extension='txt')[0]
            assert 'Acceptance page 1' in output.read_text(encoding='utf-8')
        case('extract-text',text)
        case('optimize-render-comparison',lambda:expect(fingerprint(doc('optimize')[0]),fingerprint(source)))
        def compress_images():
            noisy=root/'noise.png'
            image=Image.effect_noise((640,480),90).convert('RGB')
            image.save(noisy);image.close()
            picture=doc('images_pdf',[noisy],document_quality=100)[0]
            mixed=root/'mixed.pdf'
            writer=PdfWriter();writer.append(picture);writer.append(source);writer.write(mixed);writer.close()
            before=hashlib.sha256(mixed.read_bytes()).hexdigest()
            compressed=doc('optimize',[mixed],document_compression='jpeg',document_quality=45)[0]
            assert compressed.stat().st_size<mixed.stat().st_size*.8
            reader=PdfReader(compressed)
            expect(len(reader.pages),5)
            assert 'Acceptance page 1' in reader.pages[1].extract_text()
            expect(next(iter(reader.pages[0].images)).image.size,(640,480))
            expect(hashlib.sha256(mixed.read_bytes()).hexdigest(),before)
        case('optimize-jpeg-images-preserve-text',compress_images)
        def metadata():
            output=doc('metadata',document_metadata_mode='edit',document_title='Acceptance Unicode 日本')[0]
            expect(PdfReader(output).metadata.title,'Acceptance Unicode 日本')
            removed=doc('metadata',[output],document_metadata_mode='remove')[0]
            assert not PdfReader(removed).metadata
        case('metadata-edit-remove',metadata)
        def passwords():
            password='Synthetic PDF 日本'
            token=remember(password)
            locked=doc('protect',document_protection='set',document_output_secret=token)[0]
            reader=PdfReader(locked)
            assert reader.is_encrypted and not reader.decrypt('incorrect') and reader.decrypt(password)
            replacement=remember('Replacement fixture only')
            changed=doc('protect',[locked],document_input_secret=token,document_protection='set',document_output_secret=replacement)[0]
            changed_reader=PdfReader(changed)
            assert not changed_reader.decrypt(password) and changed_reader.decrypt('Replacement fixture only')
            unlocked=doc('protect',[changed],document_input_secret=replacement,document_protection='remove')[0]
            assert not PdfReader(unlocked).is_encrypted
            expect(fingerprint(unlocked),fingerprint(source))
        case('password-protect-change-remove',passwords)
        def reject_bad_page():
            try:doc('extract',document_pages='99')
            except ValueError as error:assert 'within' in str(error)
            else:raise AssertionError('Invalid page accepted')
        case('reject-invalid-page',reject_bad_page)
        def reject_wrong_password():
            locked=root/'locked.pdf'
            writer=PdfWriter();writer.add_blank_page(300,400);writer.encrypt('fixture only',algorithm='AES-256');writer.write(locked);writer.close()
            try:doc('extract',[locked],document_input_secret=remember('incorrect'))
            except ValueError as error:assert 'password' in str(error).lower()
            else:raise AssertionError('Wrong password accepted')
        case('reject-wrong-password',reject_wrong_password)
        def document_retry():
            output=root/'document-retry';output.mkdir()
            plan=plan_documents([FileRecord.from_path(source)],PlanRequest('documents',destination=str(output),document_task='extract',document_pages='2'))
            def stop(stage):
                if stage=='prepared':raise Cancelled('Synthetic cancellation')
            with_store=Store(output/'history')
            try:
                job=with_store.enqueue('retry','documents',document_snapshot(plan))
                execute_job(with_store,job,hook=stop)
                expect(with_store.items(job)[0]['state'],'cancelled')
                with_store.retry(job)
                expect(execute_job(with_store,job),'succeeded')
            finally:with_store.close()
            expect(widths([Path(plan.items[0].output)]),[310])
        case('document-cancel-retry',document_retry)
        tools=available_tools()
        for extension in ('pdf','txt'):
            def ocr(extension=extension):
                output=doc('ocr',[scan],extension=extension)[0]
                text=output.read_text(encoding='utf-8') if extension=='txt' else PdfReader(output).pages[0].extract_text()
                assert 'acceptance' in text and '12345' in text
            case('real-ocr-'+extension,ocr,unavailable=None if tools['tesseract'] else 'Tesseract not installed')
        def convert_office():
            output=doc('office',[office])[0]
            assert 'acceptance 12345' in PdfReader(output).pages[0].extract_text()
        case('real-office-pdf',convert_office,unavailable=None if tools['soffice'] else 'LibreOffice not installed')

        def step(operation,**options):return dict(label=operation.title(),request=asdict(PlanRequest(operation,**options)))
        def workflow(name, inputs, steps, interrupt=False):
            history=root/(name+'-history')
            store=Store(history)
            job=store.enqueue(name,'workflow',workflow_snapshot([FileRecord.from_path(p) for p in inputs],steps))
            try:
                if interrupt:
                    class Crash(BaseException):pass
                    def crash(stage):
                        if stage=='workflow_checkpoint':raise Crash()
                    try:execute_job(store,job,hook=crash)
                    except Crash:pass
                    else:raise AssertionError('Interruption hook was not reached')
                    store.close();store=Store(history)
                    store.recover();store.retry(job)
                expect(execute_job(store,job),'succeeded')
                rows=store.items(job)
                assert all(row['state']=='succeeded' for row in rows)
                output=Path(json.loads(rows[-1]['stage'])['outputs'][0]['source'])
                prior=Path(json.loads(rows[-2]['stage'])['outputs'][0]['source'])
                if output.suffix=='.zip':
                    with zipfile.ZipFile(output) as archive:expect(archive.read(prior.name),prior.read_bytes())
                else:
                    # Keep binary extraction separate from text process output.
                    from .seven_archive import seven_run
                    expect(seven_run(['x','-so','--',str(output),prior.name],None,lambda:False),prior.read_bytes())
            finally:store.close()
            reopened=Store(history)
            try:expect(reopened.visible_jobs()[0]['state'],'succeeded')
            finally:reopened.close()
        def image_flow(interrupt=False):
            workflow('image-retry' if interrupt else 'image-flow',[scan],[step('images',extension='jpg'),step('rename',prefix='Image_',padding=2),step('archives',archive_format='zip')],interrupt)
        case('workflow-image-rename-zip',image_flow)
        case('workflow-interrupt-reopen-retry',lambda:image_flow(True))
        def image_pdf_flow():
            store=Store(root/'image-pdf-history')
            try:
                steps=[step('images',extension='jpg',resize_mode='percent',resize_percent=150),
                    step('rename',prefix='Picture_',padding=2),
                    step('documents',extension='pdf',document_task='images_pdf',document_order=(str(source),str(office)))]
                job=store.enqueue('Image rename PDF','workflow',workflow_snapshot([FileRecord.from_path(scan)],steps))
                expect(execute_job(store,job),'succeeded')
                rows=store.items(job)
                output=Path(json.loads(rows[-1]['stage'])['outputs'][0]['source'])
                expect(len(PdfReader(output).pages),1)
                from .output_paths import resolved_output
                first=json.loads(rows[0]['stage'])
                moved=json.loads(rows[1]['stage'])['outputs'][0]['source']
                expect(resolved_output(store,first['child_job'],first['outputs'][0]['source']),moved)
                assert Path(moved).is_file()
            finally:store.close()
        case('workflow-image-rename-pdf-stale-order',image_pdf_flow)
        case('workflow-pdf-render-rename-zip',lambda:workflow('pdf-flow',[source],[step('documents',document_task='render',document_pages='1',extension='png'),step('rename',prefix='Page_',padding=2),step('archives',archive_format='zip')]))
        def blocked():
            with_store=Store(root/'blocked-history')
            try:
                job=with_store.enqueue('blocked','workflow',workflow_snapshot([FileRecord.from_path(scan)],[step('video',extension='mp4'),step('rename'),step('archives')]))
                execute_job(with_store,job)
                expect([r['state'] for r in with_store.items(job)],['failed','blocked','blocked'])
            finally:with_store.close()
        case('workflow-failure-blocks-dependents',blocked)
        def movie_flow():
            movie=root/'movie.mkv'
            run_process([resolve_tool('ffmpeg'),'-v','error','-f','lavfi','-i','testsrc2=size=160x90:rate=12:duration=1','-c:v','libx264','-preset','ultrafast','-threads','1',str(movie)])
            original=hashlib.sha256(movie.read_bytes()).hexdigest()
            workflow('movie-flow',[movie],[step('video',extension='mp4',video_audio_mode='none'),step('rename',prefix='Movie_',padding=2),step('archives',archive_format='7z')])
            expect(hashlib.sha256(movie.read_bytes()).hexdigest(),original)
        media_ready=all(resolve_tool(name) for name in ('ffmpeg','ffprobe','7z'))
        case('workflow-mkv-mp4-rename-7z',movie_flow,unavailable=None if media_ready else 'FFmpeg/FFprobe/7-Zip unavailable')
        case('originals-unchanged',lambda:expect({p:hashlib.sha256(p.read_bytes()).hexdigest() for p in originals},originals))
        def support_privacy():
            from .support_report import make_support_report
            store=Store(root/'support-report-history')
            try:
                marker='PRIVATE_SUPPORT_FIXTURE'
                job=store.enqueue(marker,'video',[dict(name=marker,source=str(root/marker),output=marker,
                    codec='hevc',container='mkv',crf=23,threads=2,password_token=marker,metadata={'title':marker})])
                store.set_item(store.items(job)[0]['id'],'failed',marker)
                support=make_support_report(store,job)
                assert marker not in json.dumps(support)
                assert str(root) not in json.dumps(support)
                expect(support['job']['items'][0]['settings']['codec'],'hevc')
                expect(support['job']['counts'],{'failed':1})
            finally:store.close()
        case('support-report-excludes-private-data',support_privacy)
    result=dict(version=__version__,frozen=bool(getattr(sys,'frozen',False)),platform=sys.platform,
        passed=not any(c['status']=='failed' for c in cases),counts=dict(Counter(c['status'] for c in cases)),
        cases=cases,fixtures_removed=not root.exists(),limitations='Generated inputs; optional engines unavailable are not passes. Not physical-device or manual layout acceptance.')
    report.write_text(json.dumps(result,indent=2,ensure_ascii=True),encoding='utf-8')
    if sys.stdout:print(json.dumps(dict(passed=result['passed'],counts=result['counts'])),flush=True)
    return 0 if result['passed'] else 1
