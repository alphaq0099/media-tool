"""Lazy document settings and supervised page thumbnails; no PDF import on startup."""
import base64
from pathlib import Path
from PySide6.QtCore import QThread, Signal, Qt, QSignalBlocker
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QFrame,QVBoxLayout,QLabel,QComboBox,QLineEdit,QPlainTextEdit,QSpinBox,
    QDoubleSpinBox,QPushButton,QCheckBox,QDialog,QScrollArea,QInputDialog)


class PagePreviewWorker(QThread):
    ready = Signal(object)
    failed = Signal(str)
    def __init__(self,spec,page,parent):
        super().__init__(parent)
        self.spec,self.page=spec,page
    def run(self):
        try:
            from .documents import invoke,secrets_for
            result=invoke(dict(mode="thumbnail",source=self.spec["inputs"][0]["source"],page=self.page,
                input_secret=self.spec["input_secret"],secrets=secrets_for(self.spec)),self.isInterruptionRequested)
            if not self.isInterruptionRequested():
                self.ready.emit(result)
        except Exception as error:
            if not self.isInterruptionRequested():
                self.failed.emit(str(error))


class DocumentControls:
    def __init__(self,host):
        self.host,self.worker=host,None
        self.join_order=()
        self.order_task=None
        self.order_title="Document input order"
        self._passwords={}
        self.panel=QFrame()
        self.layout=QVBoxLayout(self.panel)
        self.layout.setContentsMargins(0,0,0,0)
        self.groups={}
        self.task=QComboBox()
        for title,key in (("Merge PDFs","merge"),("Split PDF","split"),("Extract pages","extract"),("Reorder pages","reorder"),
            ("Remove pages","remove"),("Rotate pages","rotate"),("Images to PDF","images_pdf"),("PDF to images","render"),
            ("Extract PDF text","text"),("Optimize PDF","optimize"),("PDF metadata","metadata"),("PDF passwords","protect"),
            ("OCR scanned pages","ocr"),("Office to PDF","office")):
            self.task.addItem(title,key)
        self.field("Task",self.task,"always")
        self.pages=QLineEdit()
        self.pages.setPlaceholderText("All pages; e.g. 1, 3-5")
        self.pages.setToolTip("One-based page numbers. Commas and ascending/descending ranges set the order. Duplicates are rejected.")
        self.field("Pages",self.pages,"pages")
        self.split_mode=QComboBox()
        self.split_mode.addItem("One file per page","pages")
        self.split_mode.addItem("One file per range","ranges")
        self.field("Split mode",self.split_mode,"split")
        self.ranges=QPlainTextEdit()
        self.ranges.setFixedHeight(100)
        self.ranges.setPlaceholderText("1-3\n4-6")
        self.field("Page ranges (one per line)",self.ranges,"ranges")
        self.order=QPushButton("Input order…")
        self.order.clicked.connect(self.choose_order)
        self.field(None,self.order,"order")
        self.rotation=QComboBox()
        for value in (90,180,270):
            self.rotation.addItem(f"{value}° clockwise",value)
        self.field("Rotation",self.rotation,"rotation")
        self.dpi=QSpinBox()
        self.dpi.setRange(36,600)
        self.dpi.setValue(150)
        self.field("Resolution (DPI)",self.dpi,"dpi")
        self.quality=QSpinBox()
        self.quality.setRange(1,100)
        self.quality.setValue(85)
        self.field("JPEG quality",self.quality,"quality")
        self.paper=QComboBox()
        for title,key in (("A4","a4"),("Letter","letter"),("Image size at DPI","image")):
            self.paper.addItem(title,key)
        self.field("Page size",self.paper,"paper")
        self.margin=QDoubleSpinBox()
        self.margin.setRange(0,50)
        self.margin.setValue(10)
        self.field("Margins (mm)",self.margin,"paper")
        self.compression=QComboBox()
        self.compression.addItem("Lossless cleanup","lossless")
        self.compression.addItem("Compress images (JPEG)","jpeg")
        self.compression.setToolTip("Lossless cleanup compresses page data and removes duplicate objects. JPEG recompresses opaque images at the chosen quality; text and vectors stay intact. Transparent images are preserved. Smaller output is not guaranteed.")
        self.field("Compression",self.compression,"optimize")
        self.downsample=QCheckBox("Downsample large images")
        self.downsample.setToolTip("Optional quality trade-off. Transparent images are preserved. Output is not guaranteed to be smaller.")
        self.field(None,self.downsample,"optimize")
        self.image_dpi=QSpinBox()
        self.image_dpi.setRange(36,300)
        self.image_dpi.setValue(150)
        self.field("Image limit (DPI)",self.image_dpi,"downsample")
        self.metadata_mode=QComboBox()
        for title,key in (("Keep supported fields","keep"),("Edit supplied fields","edit"),("Remove fields and XMP","remove")):
            self.metadata_mode.addItem(title,key)
        self.metadata_mode.setToolTip("Keeps supported PDF document fields; XMP is removed when rewriting. This does not remove visible confidential text.")
        self.field("Metadata",self.metadata_mode,"metadata")
        self.metadata_fields={}
        for key in ("title","author","subject","keywords"):
            control=QLineEdit()
            control.setMaxLength(512)
            control.setPlaceholderText("Keep existing if blank")
            self.field(key.title(),control,"metadata_edit")
            self.metadata_fields[key]=control
        self.input_password=QLineEdit()
        self.input_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.input_password.setPlaceholderText("If input PDF is protected")
        self.input_password.setMaxLength(256)
        self.field("Input password",self.input_password,"input_password")
        self.protection=QComboBox()
        for title,key in (("Keep input protection","keep"),("Remove password","remove"),("Set / change password","set")):
            self.protection.addItem(title,key)
        self.field("Protection",self.protection,"protection")
        self.output_password=QLineEdit()
        self.output_password.setMaxLength(256)
        self.output_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.field("New password",self.output_password,"output_password")
        self.show_password=QCheckBox("Show password")
        self.show_password.toggled.connect(self.password_visible)
        self.field(None,self.show_password,"output_password")
        self.confirm=QLineEdit()
        self.confirm.setEchoMode(QLineEdit.EchoMode.Password)
        self.field("Confirm new password",self.confirm,"confirm")
        self.language=QLineEdit("eng")
        self.language.setPlaceholderText("eng or eng+hin")
        self.language.setToolTip("Install Tesseract in Tools → Dependencies, including the language data you need. Use eng for English, hin for Hindi, or eng+hin for both. Choose Searchable PDF to retain page images with selectable text, or TXT for text only. For A4 scans, try 300 DPI.")
        self.field("OCR languages",self.language,"ocr")
        self.external_hint=QLabel()
        self.external_hint.setWordWrap(True)
        self.field(None,self.external_hint,"external")
        self.external_check=QPushButton("Check optional tools")
        self.external_check.clicked.connect(self.check_external)
        self.field(None,self.external_check,"external")
        self.policy=QLabel("Originals kept. Forms/signatures and active PDFs are blocked.")
        self.policy.setWordWrap(True)
        self.policy.setToolTip("Page links/bookmarks are retained where supported by the PDF writer. Missing-page links may be omitted. PDF metadata removal is not content redaction. Text extraction keeps text rather than original layout.")
        self.layout.addWidget(self.policy)
        host.settings_layout.insertWidget(host.settings_layout.indexOf(host.keep),self.panel)
        host.format.currentIndexChanged.connect(self.changed)

    def field(self,title,control,group):
        widgets=[]
        if title:
            label=QLabel(title)
            self.layout.addWidget(label)
            widgets.append(label)
            control.setAccessibleName("Document "+title)
        self.layout.addWidget(control)
        widgets.append(control)
        self.groups.setdefault(group,[]).extend(widgets)
        signal=next((getattr(control,name) for name in ("currentIndexChanged","textChanged","valueChanged","toggled") if hasattr(control,name)),None)
        if signal:
            signal.connect(self.changed)

    def password_visible(self,visible):
        self.output_password.setEchoMode(QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password)
        self.changed()

    def changed(self,*args):
        if not hasattr(self,"policy"):
            return
        self.update_visibility()
        if getattr(self.host,"_planning_ready",False) and self.host.tool=="Documents":
            self.host.invalidate_plan()

    def update_visibility(self):
        self.panel.setVisible(self.host.tool=="Documents")
        task=self.task.currentData()
        if task!=self.order_task:
            self.join_order=()
            self.order_task=task
        formats=["PNG","JPEG"] if task=="render" else ["TXT"] if task=="text" else ["Searchable PDF","TXT"] if task=="ocr" else ["PDF"]
        if self.host.tool=="Documents" and [self.host.format.itemText(i) for i in range(self.host.format.count())]!=formats:
            with QSignalBlocker(self.host.format):
                self.host.format.clear()
                self.host.format.addItems(formats)
        visible={"always","input_password"}
        if task not in ("images_pdf","office"):
            visible.add("pages")
        if task=="split":
            visible.add("split")
            if self.split_mode.currentData()=="ranges":
                visible.add("ranges")
                visible.discard("pages")
        if task in ("merge","images_pdf"):
            visible.add("order")
        if task=="rotate":visible.add("rotation")
        if task in ("render","images_pdf","ocr"):visible.add("dpi")
        if task in ("images_pdf","render") or task=="optimize" and (self.compression.currentData()=="jpeg" or self.downsample.isChecked()):visible.add("quality")
        if task=="images_pdf":visible.add("paper")
        if task=="optimize":
            visible.add("optimize")
            if self.downsample.isChecked():visible.add("downsample")
        if task in ("metadata","optimize","protect"):
            visible.add("metadata")
            if self.metadata_mode.currentData()=="edit":visible.add("metadata_edit")
        if task=="protect":
            visible.add("protection")
            if self.protection.currentData()=="set":
                visible.add("output_password")
                if not self.show_password.isChecked():visible.add("confirm")
        if task in ("images_pdf","office"):visible.discard("input_password")
        if task=="ocr":visible.add("ocr")
        if task in ("ocr","office"):
            visible.add("external")
            self.external_hint.setText("Requires installed Tesseract and the selected language packs." if task=="ocr" else "Requires an installed LibreOffice. Fonts and page layout can differ.")
        for key,widgets in self.groups.items():
            for widget in widgets:widget.setVisible(key in visible)

    def token(self,key,value):
        from .private_values import remember
        previous=self._passwords.get(key)
        if not value:return ""
        if not previous or previous[0]!=value:
            previous=(value,remember(value))
            self._passwords[key]=previous
        return previous[1]

    def options(self):
        task=self.task.currentData()
        protection=self.protection.currentData() if task=="protect" else "keep"
        if protection=="set" and not self.show_password.isChecked() and self.confirm.text()!=self.output_password.text():
            raise ValueError("New password and confirmation do not match.")
        return dict(document_task=task,document_pages=self.pages.text(),document_split_mode=self.split_mode.currentData(),
            document_ranges=self.ranges.toPlainText(),document_order=self.join_order,document_rotation=self.rotation.currentData(),
            document_dpi=self.dpi.value(),document_quality=self.quality.value(),document_paper=self.paper.currentData(),document_margin=self.margin.value(),
            document_metadata_mode=self.metadata_mode.currentData() if task in ("metadata","optimize","protect") else "keep",
            document_protection=protection,document_input_secret=self.token("input",self.input_password.text()),
            document_output_secret=self.token("output",self.output_password.text()) if protection=="set" else "",
            document_downsample=self.downsample.isChecked(),document_compression=self.compression.currentData(),document_image_dpi=self.image_dpi.value(),document_language=self.language.text().strip(),
            **{"document_"+key:control.text() for key,control in self.metadata_fields.items()})

    def choose_order(self):
        from .audio_ui import AudioControls
        AudioControls.choose_order(self)

    def check_external(self):
        from .document_external import available_tools
        found=available_tools()
        self.host.status.setText("; ".join(name+": "+(path or "not installed") for name,path in found.items()))

    def show_page_preview(self):
        if self.host.editing_busy or not self.host.last_plan:return
        rows=self.host.preview_table.selectionModel().selectedRows()
        item=self.host.last_plan.items[rows[0].row()] if rows else self.host.last_plan.items[0]
        spec=item.document_spec
        if not spec or Path(spec["source"]).suffix.lower()!=".pdf":return
        pages=spec["inputs"][0]["pages"]
        number,accepted=QInputDialog.getInt(self.host,"Page preview","Source page",(pages[0]+1) if pages else 1,1,max(pages)+1 if pages else 1)
        if not accepted:return
        self.worker=PagePreviewWorker(spec,number-1,self.host)
        self.worker.ready.connect(self.preview_ready)
        self.worker.failed.connect(self.host.status.setText)
        self.worker.finished.connect(self.preview_finished)
        self.host.update_controls()
        self.worker.start()

    def preview_ready(self,result):
        if self.host._closing:return
        dialog=QDialog(self.host)
        dialog.setWindowTitle("PDF page preview")
        dialog.resize(750,650)
        layout=QVBoxLayout(dialog)
        scroll=QScrollArea()
        image=QLabel()
        pixmap=QPixmap()
        pixmap.loadFromData(base64.b64decode(result["png"]))
        image.setPixmap(pixmap)
        scroll.setWidget(image)
        layout.addWidget(scroll)
        close=QPushButton("Close")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close)
        dialog.exec()
        dialog.deleteLater()

    def preview_finished(self):
        worker,self.worker=self.worker,None
        worker.deleteLater()
        self.host.update_controls()
        if self.host._closing:self.host.close()
