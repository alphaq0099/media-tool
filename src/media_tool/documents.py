"""Read-only document plans and sequential, verified no-replace job execution."""
from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import sysconfig
import uuid

from .audio_edit import reserve_output
from .domain import path_key
from .images import check_parent
from .jobs import (checkpoint, check_source, cleanup_temp, commit_no_replace, hash_file,
                   identity, owns_temp, regular)
from .planning import OperationPlan, PlanItem, PlanCancelled
from .private_values import resolve
from .processes import run_process

TASKS = {"merge","split","extract","reorder","remove","rotate","images_pdf","render","text","optimize","metadata","protect","ocr","office"}


def worker_args():
    if getattr(sys,"frozen",False):
        return [sys.executable,"--document-worker"]
    paths = [str(Path(__file__).resolve().parents[1]), sysconfig.get_path("purelib")]
    # Launch the real interpreter directly, so parent-loss supervision does not
    # terminate only Windows' virtualenv launcher while leaving a PDF child alive.
    code = f"import sys;sys.path[:0]={paths!r};from media_tool.document_worker import main;raise SystemExit(main())"
    return [getattr(sys,"_base_executable",sys.executable),"-c",code]


def invoke(request, cancelled=lambda:False, progress=None):
    result = {}
    if progress is None:
        text = run_process(worker_args(), cancelled, input_text=json.dumps(request,ensure_ascii=True), timeout=60 if request["mode"] in ("probe","thumbnail") else None)
        return json.loads(text)["result"]
    def receive(line):
        value = json.loads(line)
        if "progress" in value:
            progress(min(90,10+round(float(value["progress"])*80)),100)
        elif "result" in value:
            result.update(value["result"])
    run_process(worker_args(), cancelled, input_text=json.dumps(request,ensure_ascii=True), on_line=receive)
    return result


def secrets_for(spec):
    return {token:resolve(token) for token in (spec.get("input_secret"), spec.get("output_secret")) if token}


def page_selection(text, count, default_all=True):
    if not text.strip():
        return list(range(count)) if default_all else []
    selected = []
    for part in text.split(","):
        match = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?",part)
        if not match:
            raise ValueError("Use one-based pages or ranges, for example 1, 3-5, 8.")
        start,end = int(match[1]),int(match[2] or match[1])
        if not 1 <= start <= count or not 1 <= end <= count:
            raise ValueError(f"Page selection must be within 1–{count}.")
        step = 1 if end >= start else -1
        selected.extend(range(start-1,end-1+step,step))
        if len(selected) > 5000:
            raise ValueError("At most 5000 pages can be selected.")
    if len(set(selected)) != len(selected):
        raise ValueError("Duplicate pages are not supported. List each page once.")
    return selected


def sizes_digest(sizes):
    values = [[round(float(w),2),round(float(h),2)] for w,h in sizes]
    return hashlib.sha256(json.dumps(values,separators=(",",":")).encode()).hexdigest()


def plan_documents(records, request, cancelled=lambda:False):
    task = request.document_task
    if task not in TASKS or request.collision not in ("unique","skip"):
        raise ValueError("Choose a document task and output conflict policy.")
    if not 36 <= request.document_dpi <= 600 or not 1 <= request.document_quality <= 100:
        raise ValueError("DPI must be 36–600 and image quality 1–100.")
    if request.document_rotation not in (90,180,270) or request.document_paper not in ("a4","letter","image"):
        raise ValueError("Choose a supported rotation and paper size.")
    if not math.isfinite(request.document_margin) or not 0 <= request.document_margin <= 50:
        raise ValueError("Margins must be 0–50 mm.")
    if request.document_metadata_mode not in ("keep","edit","remove") or request.document_protection not in ("keep","set","remove"):
        raise ValueError("Choose metadata and password policies.")
    if request.document_protection == "set" and not resolve(request.document_output_secret):
        raise ValueError("Enter a nonempty output PDF password.")
    if task in ("images_pdf","render","text","ocr","office") and (request.document_protection!="keep" or request.document_metadata_mode!="keep"):
        raise ValueError("Use PDF passwords or PDF metadata on the converted PDF to change these settings.")
    if not 36 <= request.document_image_dpi <= 300:
        raise ValueError("Image downsampling DPI must be 36–300.")
    if request.document_compression not in ('lossless','jpeg'):
        raise ValueError("Choose lossless cleanup or JPEG image compression.")
    metadata = dict(Title=request.document_title,Author=request.document_author,Subject=request.document_subject,Keywords=request.document_keywords)
    if any(len(v)>512 or "\0" in v for v in metadata.values()):
        raise ValueError("Document metadata must be at most 512 characters per field and contain no nulls.")
    records = tuple(sorted(records,key=lambda r:r.sort_key))
    if not records:
        raise ValueError("Add input files before previewing.")
    if len({r.path for r in records}) != len(records):
        raise ValueError("Remove duplicate inputs before previewing documents.")
    if request.document_order and task in ("merge","images_pdf"):
        order = request.document_order
        if len(order)!=len(records) or set(order)!={r.path for r in records}:
            raise ValueError("Input selection changed. Review input order again.")
        records = tuple(sorted(records,key=lambda r:order.index(r.path)))
    reserved = {path_key(r.path) for r in records if not r.sample}
    spec_base = dict(task=task,extension=request.extension,input_secret=request.document_input_secret,output_secret=request.document_output_secret,
        dpi=request.document_dpi,quality=request.document_quality,paper=request.document_paper,margin_mm=request.document_margin,
        metadata_mode=request.document_metadata_mode,metadata=metadata,protection=request.document_protection,
        downsample=request.document_downsample,compression=request.document_compression,image_dpi=request.document_image_dpi,rotation=request.document_rotation,
        language=request.document_language,keep_originals=True)
    secrets = secrets_for(spec_base)
    inspected, errors = [], []
    for record in records:
        checkpoint(cancelled)
        try:
            if record.sample:
                raise ValueError("Illustrative samples are not real document inputs.")
            source = dict(source=record.path,identity=[record.device,record.inode,record.size,record.modified_ns])
            check_source(source)
            if record.size > 512*1024**2:
                raise ValueError("Document inputs must be at most 512 MiB each.")
            image_input = task=="images_pdf" or task=="ocr" and Path(record.path).suffix.lower()!=".pdf"
            if image_input:
                from PIL import Image
                with Image.open(record.path) as image:
                    if image.width*image.height > 16_000_000 or getattr(image,"n_frames",1)!=1:
                        raise ValueError("Use single-frame images no larger than 16 megapixels.")
                    w,h = image.size
                    if image.getexif().get(274,1) in (5,6,7,8):
                        w,h = h,w
                with Image.open(record.path) as image:
                    image.verify()
                size = {"a4":(595.276,841.89),"letter":(612,792)}.get(request.document_paper,(w*72/request.document_dpi+request.document_margin*144/25.4,h*72/request.document_dpi+request.document_margin*144/25.4))
                info = dict(pages=1,sizes=[size],encrypted=False,blocked=[],metadata={})
            elif task=="office":
                if Path(record.path).suffix.lower() not in (".docx",".odt",".xlsx",".ods",".pptx",".odp"):
                    raise ValueError("Choose DOCX/ODT, XLSX/ODS or PPTX/ODP files for Office conversion.")
                from .document_external import validate_office
                validate_office(record.path)
                info = dict(pages=0,sizes=[],encrypted=False,blocked=[],metadata={})
            else:
                if Path(record.path).suffix.lower()!=".pdf":
                    raise ValueError("This task needs PDF input files.")
                info = invoke(dict(mode="probe",source=record.path,input_secret=request.document_input_secret,secrets=secrets,
                    raster_dpi=request.document_dpi if task=='render' else None),cancelled)
                if info["blocked"]:
                    raise ValueError("Unsupported PDF structures: " + ", ".join(info["blocked"]) + ". Forms, signatures and active documents are kept unchanged.")
            check_source(source)
            inspected.append((record,source,info))
        except Exception as error:
            if cancelled():
                raise PlanCancelled() from error
            errors.append(PlanItem(record,None,"Invalid",str(error)))
    if errors:
        return OperationPlan(request,tuple(errors))
    if task in ("ocr","office"):
        from .document_external import prepare_external
        spec_base.update(prepare_external(spec_base,cancelled))
    if task in ("merge","images_pdf") and len(inspected)>32:
        raise ValueError("Combine at most 32 inputs in one job.")
    jobs = []
    groups = [inspected] if task in ("merge","images_pdf") else [[entry] for entry in inspected]
    for group in groups:
        record = group[0][0]
        try:
            parent = Path(request.destination).expanduser() if request.destination.strip() else Path(record.path).parent
            if not parent.is_absolute() or not parent.is_dir():
                raise ValueError("Choose an existing absolute output folder, or leave Destination blank.")
            parent = parent.resolve(strict=True)
            part_groups = []
            if task=="split":
                count = group[0][2]["pages"]
                if request.document_split_mode=="ranges":
                    lines = [line.strip() for line in request.document_ranges.splitlines() if line.strip()]
                    if not 1<=len(lines)<=200:
                        raise ValueError("Enter 1–200 page ranges, one output range per line.")
                    parts = [page_selection(line,count) for line in lines]
                    flattened = [p for part in parts for p in part]
                    if len(flattened)!=len(set(flattened)):
                        raise ValueError("Split ranges must not overlap.")
                else:
                    parts = [[p] for p in page_selection(request.document_pages,count)]
                part_groups = [(part,index+1) for index,part in enumerate(parts)]
            elif task=="render":
                part_groups = [([p],p+1) for p in page_selection(request.document_pages,group[0][2]["pages"])]
            else:
                part_groups = [(None,None)]
            for selected, part_number in part_groups:
                spec = dict(spec_base,parent=str(parent),parent_identity=identity(parent.stat())[:2],source=record.path,
                    identity=group[0][1]["identity"],name=record.name,inputs=[],input_encrypted=any(info["encrypted"] for _,_,info in group))
                expected = []
                for _,source,info in group:
                    if task in ("images_pdf","office"):
                        pages = list(range(info["pages"]))
                    else:
                        pages = selected if selected is not None else page_selection(request.document_pages,info["pages"])
                        if task=="remove":
                            if not request.document_pages.strip():
                                raise ValueError("Choose pages to remove.")
                            pages = [p for p in range(info["pages"]) if p not in pages]
                        elif task=="rotate":
                            spec["rotate_pages"] = pages
                            pages = list(range(info["pages"]))
                    if not pages and task!="office":
                        raise ValueError("The operation would create an empty document.")
                    spec["inputs"].append(dict(source,pages=pages))
                    expected.extend(info["sizes"][p] for p in pages)
                if len(expected)>5000:
                    raise ValueError("A PDF output is limited to 5000 pages.")
                if task=="rotate":
                    for index in spec["rotate_pages"]:
                        if request.document_rotation%180:
                            expected[index] = list(reversed(expected[index]))
                spec["page_count"],spec["sizes_digest"] = len(expected),sizes_digest(expected)
                spec["expected_metadata"] = {k:v for k,v in group[0][2]["metadata"].items() if k in ("/Title","/Author","/Subject","/Keywords","/Creator","/Producer","/CreationDate","/ModDate")}
                if spec["metadata_mode"]=="remove":
                    spec["expected_metadata"] = {}
                elif spec["metadata_mode"]=="edit":
                    spec["expected_metadata"].update({"/"+k:v for k,v in metadata.items() if v})
                extension = request.extension if task in ("render","ocr") else "txt" if task=="text" else "pdf"
                if task=="render":
                    if extension not in ("png","jpg"):
                        raise ValueError("Choose PNG or JPEG for page export.")
                    spec["page"] = selected[0]
                    spec['raster_size']=group[0][2]['raster_sizes'][selected[0]]
                    spec['raster_geometry']='pdfium'
                    if math.prod(spec["raster_size"])>16_000_000:
                        raise ValueError("This DPI exceeds the 16-megapixel page limit. Lower DPI.")
                if task=="ocr" and Path(record.path).suffix.lower()==".pdf" and any(math.ceil(w*request.document_dpi/72)*math.ceil(h*request.document_dpi/72)>16_000_000 for w,h in expected):
                    raise ValueError("OCR at this DPI exceeds the 16-megapixel page limit. Lower DPI.")
                spec["extension"] = extension
                suffix = f"_page{part_number:04d}" if task=="render" else f"_part{part_number:03d}" if task=="split" else "_"+task
                output,status = reserve_output(parent,f"{Path(record.name).stem}{suffix}.{extension}",request.collision,reserved,cancelled)
                spec["output"] = output
                reason = f"{len(spec['inputs'])} input(s); {spec['page_count']} page(s); " + "; ".join(Path(s["source"]).name+": "+",".join(str(p+1) for p in s["pages"]) for s in spec["inputs"])
                if task=="render":
                    reason += f"; {spec['raster_size'][0]}×{spec['raster_size'][1]} px; {request.document_dpi} DPI"
                jobs.append(PlanItem(record,output,status,reason if status=="Planned" else "Output already exists.",document_spec=spec))
        except Exception as error:
            if cancelled():
                raise PlanCancelled() from error
            jobs.append(PlanItem(record,None,"Invalid",str(error)))
    warnings = ("Order: "+" → ".join(r.name for r,_,_ in inspected),) if task in ("merge","images_pdf") else ()
    return OperationPlan(request,tuple(jobs),warnings)


def document_snapshot(plan):
    if plan.request.operation!="documents" or any(i.status not in ("Planned","Skipped") for i in plan.items) or not any(i.status=="Planned" for i in plan.items):
        raise ValueError("Resolve invalid inputs and preview documents before queuing.")
    return tuple(json.loads(json.dumps(i.document_spec)) if i.status=="Planned" else dict(source=i.source.path,name=i.source.name,identity=[i.source.device,i.source.inode,i.source.size,i.source.modified_ns],output=i.output,initial_state="skipped",initial_message=i.reason) for i in plan.items)


def run_document(store,item,cancelled,progress,hook=lambda point:None):
    spec = json.loads(item["spec"])
    from .document_external import cleanup_scope,create_scope
    cleanup_scope(store,item["id"])
    parent,output = check_parent(spec),Path(spec["output"])
    if item["digest"] and owns_temp(item,output):
        if hash_file(output,cancelled)!=item["digest"]:
            raise ValueError("Previously committed document changed; it was left untouched.")
        cleanup_temp(store,item["id"])
        return "Recovered verified document. Originals kept."
    if item["temp"] and os.path.lexists(item["temp"]) and not cleanup_temp(store,item["id"]):
        raise ValueError("Temporary document ownership changed; it was kept.")
    secrets = secrets_for(spec)
    originals = []
    for source in spec["inputs"]:
        check_source(source)
        originals.append(hash_file(source["source"],cancelled))
    if os.path.lexists(output):
        raise FileExistsError("Output appeared after preview; existing file was kept.")
    temp = parent/f".mediatool-{item['job_id']}-{item['id']}-{uuid.uuid4().hex}.part"
    with temp.open("xb") as handle:
        info = os.fstat(handle.fileno())
        store.set_item(item["id"],"running","Processing document",temp=str(temp),temp_dev=str(info.st_dev),temp_ino=str(info.st_ino),digest=None)
    hook("created")
    checkpoint(cancelled)
    if not owns_temp(store.item(item['id'])):
        raise ValueError('Temporary document ownership changed; file was kept.')
    spec['temp_identity']=identity(info)[:2]
    if spec["task"] in ("ocr","office"):
        scope=create_scope(store,item["id"])
        runtime_spec=dict(spec,scratch=scope["path"])
        try:
            runtime=invoke(dict(mode="run",spec=runtime_spec,output=str(temp),secrets=secrets),cancelled,progress)
        except BaseException:
            try:cleanup_scope(store,item["id"])
            except Exception:pass
            raise
        cleanup_scope(store,item["id"])
        if spec["task"]=="office":spec["page_count"]=runtime["page_count"]
    else:
        invoke(dict(mode="run",spec=spec,output=str(temp),secrets=secrets),cancelled,progress)
    hook("written")
    if not owns_temp(store.item(item["id"])):
        raise ValueError("Temporary output was replaced; it was kept.")
    with temp.open("rb+") as handle:
        os.fsync(handle.fileno())
    store.set_item(item["id"],"verifying","Reopening output and checking originals")
    invoke(dict(mode="verify",spec=spec,output=str(temp),secrets=secrets),cancelled,lambda *args:None)
    for source,digest in zip(spec["inputs"],originals):
        check_source(source)
        if hash_file(source["source"],cancelled)!=digest:
            raise ValueError("An original changed while processing. No final output was saved.")
    verified = identity(regular(temp))
    if not owns_temp(store.item(item['id'])):
        raise ValueError('Temporary document ownership changed during verification; file was kept.')
    digest = hash_file(temp,cancelled)
    store.set_item(item["id"],"committing","Verified document; committing output",digest=digest,source_digest=originals[0])
    hook("prepared")
    checkpoint(cancelled)
    check_parent(spec)
    for source in spec["inputs"]:
        check_source(source)
    if identity(regular(temp))!=verified:
        raise ValueError("Temporary document changed after verification.")
    commit_no_replace(temp,output)
    hook("committed")
    progress(100,100)
    return f"{spec['task']}: {spec['page_count']} page(s); {sum(s['identity'][2] for s in spec['inputs']):,} → {output.stat().st_size:,} bytes. All original SHA-256 hashes unchanged."
