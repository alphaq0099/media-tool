"""PDF operations imported only inside a supervised worker process."""
import base64
import hashlib
from io import BytesIO
import json
import math
import os
from pathlib import Path

MAX_PAGES = 5000
MAX_PIXELS = 16_000_000


def owned_output(spec, path, text=False):
    """Open without truncating, then verify the actual handle before writing."""
    from .jobs import identity,regular
    expected=spec['temp_identity']
    if identity(regular(path))[:2]!=expected:
        raise ValueError('Temporary document ownership changed; file was kept.')
    handle=open(path,'rb+')
    try:
        if identity(os.fstat(handle.fileno()))[:2]!=expected:
            raise ValueError('Temporary document was replaced while opening; file was kept.')
        handle.truncate(0)
        if text:
            from io import TextIOWrapper
            return TextIOWrapper(handle,encoding='utf-8',newline='\n')
        return handle
    except BaseException:
        handle.close()
        raise


def reader(path, password=""):
    from pypdf import PdfReader
    pdf = PdfReader(path, strict=False)
    if pdf.is_encrypted and not pdf.decrypt(password):
        raise ValueError("Input PDF password is missing or incorrect.")
    return pdf


def inspect_pdf(path, password="", raster_dpi=None):
    pdf = reader(path, password)
    count = len(pdf.pages)
    if not 1 <= count <= MAX_PAGES:
        raise ValueError(f"PDF must have 1–{MAX_PAGES} pages.")
    catalog = pdf.trailer["/Root"]
    form = catalog.get("/AcroForm")
    form = form.get_object() if form else {}
    flags = []
    if form.get("/Fields") or form.get("/XFA"):
        flags.append("interactive forms/signatures")
    if any(key in catalog for key in ('/Perms','/DSS')):
        flags.append('signature/certification structures')
    names = catalog.get("/Names")
    names = names.get_object() if names else {}
    if any(k in catalog for k in ("/OpenAction", "/AA", "/Collection")) or any(k in names for k in ("/JavaScript", "/EmbeddedFiles")):
        flags.append("active content, attachments or portfolio")
    if any("/AA" in p or "/Metadata" in p for p in pdf.pages):
        flags.append("page actions or page metadata")
    for page in pdf.pages:
        for reference in page.get("/Annots",[]):
            annotation=reference.get_object()
            if annotation.get('/Subtype')=='/Widget':
                flags.append('interactive form widgets')
            action=annotation.get("/A")
            action=action.get_object() if action else {}
            if "/AA" in annotation or action.get("/S") not in (None,"/URI","/GoTo"):
                flags.append("active annotations")
    sizes = []
    for page in pdf.pages:
        w,h = float(page.cropbox.width), float(page.cropbox.height)
        if w <= 0 or h <= 0 or not math.isfinite(w+h):
            raise ValueError("PDF page dimensions are invalid.")
        if page.rotation % 180:
            w,h = h,w
        sizes.append([round(w,4), round(h,4)])
    metadata = {str(k):str(v) for k,v in (pdf.metadata or {}).items() if isinstance(v, str)}
    result=dict(pages=count, sizes=sizes, encrypted=pdf.is_encrypted, blocked=flags, metadata=metadata)
    if raster_dpi is not None:
        import pypdfium2 as pdfium
        result['raster_sizes']=[]
        with pdfium.PdfDocument(path,password=password or None) as rendered:
            if len(rendered)!=count:raise ValueError('PDF readers disagree on page count.')
            for index in range(count):
                page=rendered[index]
                try:result['raster_sizes'].append(raster_dimensions(page,raster_dpi))
                finally:page.close()
    return result


def raster_dimensions(page,dpi):
    # PDFium uses floating-point visible page dimensions, which can differ from
    # the rounded decimal CropBox dimensions used for PDF structure rewriting.
    w,h=page.get_size()
    if not math.isfinite(w+h) or min(w,h)<=0:
        raise ValueError('PDF raster dimensions are invalid.')
    return [math.ceil(w*dpi/72),math.ceil(h*dpi/72)]


def raster(path, page_number, dpi, password=""):
    import pypdfium2 as pdfium
    with pdfium.PdfDocument(path, password=password or None) as pdf:
        page = pdf[page_number]
        try:
            width,height = raster_dimensions(page,dpi)
            if width*height > MAX_PIXELS or width <= 0 or height <= 0:
                raise ValueError("Raster exceeds the 16-megapixel page limit. Lower DPI.")
            bitmap = page.render(scale=dpi/72)
            try:
                return bitmap.to_pil().convert("RGB").copy()
            finally:
                bitmap.close()
        finally:
            page.close()


def image_pdf(spec, output):
    import pypdfium2 as pdfium
    from PIL import Image, ImageOps
    pdf = pdfium.PdfDocument.new()
    try:
        for index, source in enumerate(spec["inputs"]):
            with Image.open(source["source"]) as raw:
                if raw.width*raw.height > MAX_PIXELS or getattr(raw,"n_frames",1) != 1:
                    raise ValueError("Images must be single-frame and at most 16 megapixels.")
                image = ImageOps.exif_transpose(raw).convert("RGBA")
                background = Image.new("RGB", image.size, "white")
                background.paste(image, mask=image.getchannel("A"))
                image.close()
                image = background
                margin = spec["margin_mm"]*72/25.4
                paper = {"a4":(595.276,841.89), "letter":(612.,792.)}.get(spec["paper"])
                w,h = paper or (image.width*72/spec["dpi"]+2*margin,image.height*72/spec["dpi"]+2*margin)
                scale = min((w-2*margin)/image.width,(h-2*margin)/image.height)
                if scale <= 0:
                    raise ValueError("Margins leave no room for the image.")
                buffer = BytesIO()
                image.save(buffer,format="JPEG",quality=spec["quality"],subsampling=0)
                iw,ih = image.size
                image.close()
            page = pdf.new_page(w,h)
            picture = pdfium.PdfImage.new(pdf)
            buffer.seek(0)
            picture.load_jpeg(buffer, inline=True)
            picture.set_matrix(pdfium.PdfMatrix(iw*scale,0,0,ih*scale,(w-iw*scale)/2,(h-ih*scale)/2))
            page.insert_obj(picture)
            page.gen_content()
            page.close()
            print(json.dumps(dict(progress=(index+1)/len(spec["inputs"])*.7)),flush=True)
        with owned_output(spec,output) as handle:
            pdf.save(handle)
    finally:
        pdf.close()


def rewrite(spec, secrets, output):
    from pypdf import PdfWriter
    writer = PdfWriter()
    readers = []
    password = secrets.get(spec.get("input_secret"), "")
    for part in spec["inputs"]:
        pdf = reader(part["source"], password)
        readers.append(pdf)
        if inspect_pdf(part["source"], password)["blocked"]:
            raise ValueError("This PDF has unsupported interactive/active structures. Originals were kept.")
        writer.append(pdf, pages=part["pages"], import_outline=True)
    if spec["task"] == "rotate":
        for index in spec["rotate_pages"]:
            writer.pages[index].rotate(spec["rotation"])
    if spec["task"] == "optimize":
        writer.compress_identical_objects(remove_duplicates=True, remove_unreferenced=True)
        for index,page in enumerate(writer.pages):
            page.compress_content_streams(level=6)
            if spec["downsample"] or spec.get('compression')=='jpeg':
                max_size = (max(1,round(float(page.mediabox.width)*spec["image_dpi"]/72)), max(1,round(float(page.mediabox.height)*spec["image_dpi"]/72)))
                for item in page.images:
                    image = item.image
                    if image.width*image.height > MAX_PIXELS:
                        raise ValueError("An embedded image exceeds the optimization pixel limit.")
                    if image.mode in ("RGBA","LA","P"):
                        continue
                    resize=spec['downsample'] and (image.width > max_size[0] or image.height > max_size[1])
                    if resize or spec.get('compression')=='jpeg':
                        replacement=image.convert('RGB')
                        try:
                            if resize:replacement.thumbnail(max_size)
                            item.replace(replacement, quality=spec["quality"])
                        finally:replacement.close()
            print(json.dumps(dict(progress=(index+1)/len(writer.pages)*.7)),flush=True)
    metadata = {}
    if spec["metadata_mode"] != "remove":
        supported = ("/Title","/Author","/Subject","/Keywords","/Creator","/Producer","/CreationDate","/ModDate")
        metadata = {k:str(v) for k,v in (readers[0].metadata or {}).items() if k in supported and isinstance(v,str)}
        if spec["metadata_mode"] == "edit":
            metadata.update({"/"+k:v for k,v in spec["metadata"].items() if v})
    writer.metadata = metadata or None
    writer._root_object.pop("/Metadata", None)
    output_password = secrets.get(spec.get("output_secret"), "")
    if spec["protection"] == "keep" and spec["input_encrypted"]:
        output_password = password
    if spec["protection"] == "set" or spec["protection"] == "keep" and spec["input_encrypted"]:
        if not output_password:
            raise ValueError("A nonempty password is required to keep/set encryption.")
        writer.encrypt(output_password, algorithm="AES-256")
    with owned_output(spec,output) as handle:
        writer.write(handle)
    writer.close()


def dispatch(request):
    mode = request["mode"]
    secrets = request.get("secrets",{})
    if mode == "probe":
        return inspect_pdf(request["source"], secrets.get(request.get("input_secret"), ""),request.get('raster_dpi'))
    if mode == "thumbnail":
        image = raster(request["source"], request["page"], 70, secrets.get(request.get("input_secret"), ""))
        image.thumbnail((700,700))
        buffer = BytesIO()
        image.save(buffer,format="PNG")
        image.close()
        return dict(png=base64.b64encode(buffer.getvalue()).decode("ascii"))
    spec = request["spec"]
    output = request["output"]
    task = spec["task"]
    password = secrets.get(spec.get("input_secret"), "")
    if mode == "verify":
        if spec["extension"] in ("png","jpg"):
            from PIL import Image
            expected=spec['raster_size']
            if spec.get('raster_geometry')!='pdfium':
                # Existing queued jobs used pypdf's rounded CropBox estimate.
                # Retry verifies against the frozen source's actual render size,
                # rather than accepting an arbitrary +/- one-pixel tolerance.
                import pypdfium2 as pdfium
                with pdfium.PdfDocument(spec['inputs'][0]['source'],password=password or None) as pdf:
                    page=pdf[spec['page']]
                    try:expected=raster_dimensions(page,spec['dpi'])
                    finally:page.close()
            with Image.open(output) as image:
                image.load()
                wanted='JPEG' if spec['extension']=='jpg' else 'PNG'
                if list(image.size)!=expected or image.format!=wanted:
                    raise ValueError(f'Rendered page verification failed: expected {expected[0]}×{expected[1]} {wanted}, got {image.width}×{image.height} {image.format}.')
        elif spec["extension"]=="txt":
            if Path(output).stat().st_size > 64*1024**2:
                raise ValueError("Text output exceeds 64 MiB.")
            if not Path(output).read_text(encoding="utf-8").strip():
                raise ValueError("Text output is empty.")
        else:
            from .documents import sizes_digest
            expected_encrypted = spec["protection"]=="set" or spec["protection"]=="keep" and spec["input_encrypted"]
            output_password = secrets.get(spec.get("output_secret"),"") if spec["protection"]=="set" else password if expected_encrypted else ""
            actual = inspect_pdf(output,output_password)
            if task!="office" and actual["pages"]!=spec["page_count"]:
                raise ValueError("PDF page count failed verification.")
            if task not in ("office","ocr") and sizes_digest(actual["sizes"])!=spec["sizes_digest"]:
                raise ValueError("PDF page count, order geometry or rotation failed verification.")
            if actual["encrypted"]!=bool(expected_encrypted):
                raise ValueError("PDF encryption policy failed verification.")
            if task not in ("images_pdf","office","ocr"):
                for key,value in spec["expected_metadata"].items():
                    if actual["metadata"].get(key)!=value:
                        raise ValueError("Document metadata failed verification.")
                if spec["metadata_mode"]=="remove" and actual["metadata"]:
                    raise ValueError("Document metadata was not removed.")
            import pypdfium2 as pdfium
            with pdfium.PdfDocument(output,password=output_password or None) as pdf:
                if len(pdf)!=actual["pages"]:
                    raise ValueError("Independent PDF reader disagrees on page count.")
        return dict(verified=True)
    if task == "images_pdf":
        image_pdf(spec, output)
    elif task in ("office","ocr"):
        from .document_external import run_external
        return run_external(spec,output,secrets)
    elif task == "render":
        image = raster(spec["inputs"][0]["source"], spec["page"], spec["dpi"], password)
        with owned_output(spec,output) as handle:
            image.save(handle,format="JPEG" if spec["extension"] == "jpg" else "PNG", **({"quality":spec["quality"]} if spec["extension"] == "jpg" else {}))
        image.close()
    elif task == "text":
        import pypdfium2 as pdfium
        any_text = False
        with pdfium.PdfDocument(spec["inputs"][0]["source"], password=password or None) as pdf, owned_output(spec,output,text=True) as handle:
            for ordinal,index in enumerate(spec["inputs"][0]["pages"]):
                page = pdf[index]
                try:
                    textpage = page.get_textpage()
                    try:
                        text = textpage.get_text_range()
                    finally:
                        textpage.close()
                finally:
                    page.close()
                if len(text) > 8*1024**2:
                    raise ValueError("Extracted page text exceeds the supported limit.")
                any_text = any_text or bool(text.strip())
                handle.write(("\n\f\n" if ordinal else "")+text)
                if handle.tell()>64*1024**2:
                    raise ValueError("Text output exceeds 64 MiB.")
                print(json.dumps(dict(progress=(ordinal+1)/len(spec["inputs"][0]["pages"])*.8)),flush=True)
        if not any_text:
            raise ValueError("No existing text layer was found. Choose OCR for scanned pages.")
    else:
        rewrite(spec,secrets,output)
    return dict(task=task)
