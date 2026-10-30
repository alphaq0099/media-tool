"""Optional, separately installed OCR/Office tools and owned workspaces."""
import json
import os
from pathlib import Path
import re
import shutil
import stat
import uuid
import zipfile

from .jobs import identity,regular
from .processes import run_process


def available_tools():
    from .tool_paths import resolve_tool
    return {name:resolve_tool(name) for name in ('tesseract','soffice')}


def prepare_external(spec,cancelled):
    name="tesseract" if spec["task"]=="ocr" else "soffice"
    path=available_tools()[name]
    if not path:
        raise ValueError("Install Tesseract and the required language packs separately, then retry OCR." if name=="tesseract" else "Install LibreOffice separately, then retry Office to PDF. No office suite is bundled.")
    path=str(Path(path).resolve(strict=True))
    result=dict(external_tool=path,external_identity=identity(regular(path)))
    if name=="tesseract":
        language=spec["language"]
        if not re.fullmatch(r"[A-Za-z0-9_]{1,30}(?:\+[A-Za-z0-9_]{1,30}){0,5}",language):
            raise ValueError("Use OCR language codes such as eng or eng+hin.")
        if spec["extension"] not in ("pdf","txt"):
            raise ValueError("Choose Searchable PDF or TXT for OCR.")
        text=run_process([path,"--list-langs"],cancelled,timeout=20)
        present=set(text.splitlines())
        missing=[v for v in language.split("+") if v not in present]
        if missing:raise ValueError("Missing installed OCR language packs: "+", ".join(missing))
    else:
        run_process([path,"--version"],cancelled,timeout=20)
    return result


def directory_identity(path):
    info=Path(path).lstat()
    if not stat.S_ISDIR(info.st_mode) or getattr(info,"st_file_attributes",0)&0x400:
        raise ValueError("Document workspace was replaced by a link or non-directory; it was kept.")
    return identity(info)[:2]


def create_scope(store,item_id):
    nonce=uuid.uuid4().hex
    root=store.directory.resolve()/f".mediatool-doc-{item_id}-{nonce}"
    root.mkdir(mode=0o700)
    marker=root/".owner"
    with marker.open("x",encoding="ascii") as handle:handle.write(nonce)
    scope=dict(path=str(root),identity=directory_identity(root),nonce=nonce,marker_identity=identity(marker.stat())[:2])
    item=store.item(item_id)
    store.set_item(item_id,item["state"],item["message"],stage=json.dumps(scope))
    return scope


def cleanup_scope(store,item_id):
    item=store.item(item_id)
    if not item["stage"]:return
    scope=json.loads(item["stage"])
    root=Path(scope["path"])
    if root.parent!=store.directory.resolve() or not root.name.startswith(f".mediatool-doc-{item_id}-"):
        raise ValueError("Document workspace is outside its recorded history directory.")
    if root.exists():
        if directory_identity(root)!=scope["identity"]:raise ValueError("Document workspace ownership changed; it was kept.")
        marker=root/".owner"
        if identity(regular(marker))[:2]!=scope["marker_identity"] or marker.read_text(encoding="ascii")!=scope["nonce"]:
            raise ValueError("Document workspace marker changed; files were kept.")
        directories,files=[],[]
        stack=[root]
        while stack:
            current=stack.pop()
            directories.append((current,directory_identity(current)))
            for entry in current.iterdir():
                info=entry.lstat()
                if entry.is_symlink() or getattr(info,"st_file_attributes",0)&0x400:
                    raise ValueError("A link appeared in the document workspace; files were kept.")
                if stat.S_ISDIR(info.st_mode):stack.append(entry)
                elif stat.S_ISREG(info.st_mode):files.append((entry,identity(info)[:2]))
                else:raise ValueError("Unexpected entry in document workspace; it was kept.")
        for path,owned in files:
            if identity(regular(path))[:2]!=owned:raise ValueError("Document work file was replaced; it was kept.")
            path.unlink()
        for path,owned in reversed(directories):
            if directory_identity(path)!=owned:raise ValueError("Document work directory changed; it was kept.")
            path.rmdir()
    store.set_item(item_id,item["state"],item["message"],stage=None)


def validate_office(path):
    with zipfile.ZipFile(path) as archive:
        entries=archive.infolist()
        if len(entries)>10000 or sum(i.file_size for i in entries)>512*1024**2:
            raise ValueError("Office archive exceeds the supported content limit.")
        for item in entries:
            name=item.filename.casefold()
            if "vbaproject" in name or name.startswith(("basic/","scripts/")):
                raise ValueError("Office files containing macros need manual conversion.")
            if item.file_size>64*1024**2:
                raise ValueError("An Office component exceeds 64 MiB.")
            if name.endswith((".rels",".xml")):
                text=archive.read(item).decode("utf-8",errors="replace").casefold()
                if re.search(r'targetmode\s*=\s*[\'"]external',text) or re.search(r'xlink:href\s*=\s*[\'"](?:https?:|file:|ftp:|//)',text) or "encryption-data" in text or "macro:" in text:
                    raise ValueError("Office files with external links, macros or encryption need manual conversion.")


def run_external(spec,output,secrets):
    from .pdf_backend import raster,reader,inspect_pdf,owned_output
    if identity(regular(spec["external_tool"]))!=spec["external_identity"]:
        raise ValueError("The external document tool changed after preview.")
    root=Path(spec["scratch"])
    if spec["task"]=="office":
        source=Path(spec["inputs"][0]["source"])
        validate_office(source)
        profile=root/"profile"
        (profile/"user").mkdir(parents=True)
        (profile/"user"/"registrymodifications.xcu").write_text('''<?xml version="1.0" encoding="UTF-8"?><oor:items xmlns:oor="http://openoffice.org/2001/registry"><item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item></oor:items>''',encoding="utf-8")
        destination=root/"converted"
        destination.mkdir()
        run_process([spec["external_tool"],"-env:UserInstallation="+profile.as_uri(),"--headless","--nologo","--nodefault",
            "--norestore","--convert-to","pdf","--outdir",str(destination),str(source)],timeout=180)
        converted=destination/(source.stem+".pdf")
        if not converted.is_file():raise ValueError("LibreOffice did not produce a PDF. Check the input format, fonts and print settings.")
        info=inspect_pdf(str(converted))
        with converted.open('rb') as source_handle, owned_output(spec,output) as output_handle:
            shutil.copyfileobj(source_handle,output_handle,1024*1024)
        return dict(page_count=info["pages"])
    from pypdf import PdfWriter
    from PIL import Image,ImageOps
    source=spec["inputs"][0]
    password=secrets.get(spec.get("input_secret"),"")
    writer=PdfWriter() if spec["extension"]=="pdf" else None
    text_output=owned_output(spec,output,text=True) if writer is None else None
    recognized=False
    pages=source["pages"]
    for ordinal,index in enumerate(pages):
        png=root/f"page-{ordinal:05d}.png"
        if Path(source["source"]).suffix.lower()==".pdf":
            image=raster(source["source"],index,spec["dpi"],password)
        else:
            with Image.open(source["source"]) as raw:
                image=ImageOps.exif_transpose(raw).convert("RGB")
        image.save(png,format="PNG",dpi=(spec["dpi"],spec["dpi"]))
        image.close()
        base=root/f"ocr-{ordinal:05d}"
        run_process([spec["external_tool"],str(png),str(base),"-l",spec["language"],"--dpi",str(spec["dpi"]),"pdf" if writer is not None else "txt"],timeout=180)
        png.unlink()
        if writer is not None:
            output_page=reader(str(base.with_suffix(".pdf")))
            recognized=recognized or any(p.extract_text().strip() for p in output_page.pages)
            writer.append(output_page)
        else:
            text=base.with_suffix(".txt").read_text(encoding="utf-8")
            recognized=recognized or bool(text.strip())
            text_output.write(("\n\f\n" if ordinal else "")+text)
            if text_output.tell()>64*1024**2:raise ValueError("OCR text exceeds 64 MiB.")
        print(json.dumps(dict(progress=(ordinal+1)/len(pages)*.8)),flush=True)
    if text_output is not None:text_output.close()
    if not recognized:raise ValueError("No text was recognized. Check the scan and selected OCR languages.")
    if writer is not None:
        if spec["protection"]=="keep" and spec["input_encrypted"]:
            writer.encrypt(password,algorithm="AES-256")
        with owned_output(spec,output) as handle:
            writer.write(handle)
        writer.close()
    return dict(page_count=len(pages))
