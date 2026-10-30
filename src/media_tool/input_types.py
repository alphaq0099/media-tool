"""Tool input scopes and extension choices, without decoding media on the GUI thread."""
from PySide6.QtCore import QSignalBlocker,Qt
from PySide6.QtWidgets import QWidget,QHBoxLayout,QLabel

from .domain import FilterSpec,AUDIO_EXTS,VIDEO_EXTS
from .help_ui import InfoButton

# Match the still-image decoder's current supported formats, not every extension
# classified as an image (HEIC, GIF and AVIF cannot be converted by this release).
IMAGES=frozenset(('.jpg','.jpeg','.png','.webp','.bmp','.tif','.tiff'))
VIDEO=frozenset(VIDEO_EXTS)
AUDIO=frozenset(AUDIO_EXTS)
OFFICE=frozenset(('.docx','.odt','.xlsx','.ods','.pptx','.odp'))
DOCUMENTS=frozenset(('.pdf',))|IMAGES|OFFICE
SCOPES={'Images':IMAGES,'Video':VIDEO,'Audio':AUDIO|VIDEO,'Documents':DOCUMENTS}
POPULAR={
    'Images':(('PNG',('.png',)),('JPG / JPEG',('.jpg','.jpeg')),('WebP',('.webp',))),
    'Video':(('MP4',('.mp4',)),('MKV',('.mkv',)),('MOV',('.mov',)),('WebM',('.webm',)),('AVI',('.avi',))),
    'Audio':(('MP3',('.mp3',)),('WAV',('.wav',)),('FLAC',('.flac',)),('M4A',('.m4a',)),('AAC',('.aac',)),('OGG / Opus',('.ogg','.opus'))),
    'Documents':(('PDF',('.pdf',)),('Image sources',tuple(sorted(IMAGES))),('Office sources',tuple(sorted(OFFICE)))),
}
CATEGORIES=('All types','Image','Video','Audio','Document','Archive','Folder','Other')


class InputTypes:
    def __init__(self,host):
        self.host=host
        self.tool=None
        self.states={}
        self.choices={}
        self.notice=QWidget()
        row=QHBoxLayout(self.notice)
        row.setContentsMargins(0,0,0,0)
        self.message=QLabel()
        self.message.setObjectName('warning')
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        row.addWidget(self.message,1)
        row.addWidget(InfoButton(self.information,name='Input type information'))
        host.visual.files_page.layout().addWidget(self.notice)
        self.notice.hide()

    def configure(self,tool):
        host=self.host
        if self.tool:
            self.states[self.tool]=(host.category_filter.currentText(),host.name_filter.text(),host.extension_filter.text())
        self.tool=tool
        blockers=[QSignalBlocker(widget) for widget in (host.category_filter,host.name_filter,host.extension_filter)]
        host.category_filter.clear()
        self.choices={}
        if tool in SCOPES:
            base=AUDIO if tool=='Audio' else SCOPES[tool]
            title={'Images':'All images','Video':'All videos','Audio':'All audio','Documents':'All documents'}[tool]
            self.choices[title]=base
            for label,extensions in POPULAR[tool]:self.choices[label]=frozenset(extensions)
            common=frozenset(ext for _,extensions in POPULAR[tool] for ext in extensions)
            if base-common:self.choices['Other supported']=base-common
            if tool=='Audio':
                self.choices['Video sources']=VIDEO
                self.choices['Audio + video']=AUDIO|VIDEO
            host.category_filter.addItems(list(self.choices))
            host.category_filter.setAccessibleName('Filter input extensions')
            host.category_filter.setToolTip(self.information())
        else:
            host.category_filter.addItems(CATEGORIES)
            host.category_filter.setAccessibleName('Filter by category')
            host.category_filter.setToolTip('Filter the listed input categories.')
        selection,name,extensions=self.states.get(tool,('', '', ''))
        index=host.category_filter.findText(selection)
        host.category_filter.setCurrentIndex(max(0,index))
        host.name_filter.setText(name)
        host.extension_filter.setText(extensions)
        del blockers

    def spec(self):
        host=self.host
        if self.tool not in SCOPES:
            return FilterSpec.parse(host.category_filter.currentText(),host.name_filter.text(),host.extension_filter.text())
        allowed=self.choices[host.category_filter.currentText()]
        return FilterSpec.parse(name=host.name_filter.text(),extensions=host.extension_filter.text(),allowed_extensions=allowed)

    def total(self):
        host=self.host
        if self.tool not in SCOPES:return host.model.rowCount()
        choice=host.category_filter.currentText()
        base=(self.choices[choice] if self.tool=='Audio' and choice in ('Video sources','Audio + video') else
            AUDIO if self.tool=='Audio' else SCOPES[self.tool])
        return sum(host.model.extension_counts[ext] for ext in base)

    def refresh(self):
        host=self.host
        lines=[]
        if self.tool in SCOPES:
            counts=host.model.extension_counts
            unsupported=sum(count for ext,count in counts.items() if ext not in SCOPES[self.tool])
            if unsupported:
                lines.append(f'{unsupported:,} file'+(' is' if unsupported==1 else 's are')+f' not supported in {self.tool} and '+('is' if unsupported==1 else 'are')+' hidden.')
            if self.tool=='Audio' and not (self.choices[host.category_filter.currentText()] & VIDEO):
                videos=sum(counts[ext] for ext in VIDEO)
                if videos:lines.append(f'{videos:,} video source'+(' is' if videos==1 else 's are')+' available under “Video sources”.')
        self.message.setText(' '.join(lines))
        self.notice.setVisible(bool(lines))

    def information(self):
        text='Only supported input extensions appear in each media tool. Preview still validates file contents. Other supported groups the less common extensions. Filters affect Preview and Start. Inputs remain available when switching tools; no files are changed.'
        if self.tool=='Images':text+=' Images accepts PNG, JPEG, WebP, BMP and single-page TIFF. Animated images, GIF, AVIF and HEIC are not supported in this release.'
        elif self.tool=='Audio':text+=' All audio lists sound files. Choose Video sources to extract audio from videos, or Audio + video to process a mixed selection, including Join.'
        elif self.tool=='Documents':text+=' PDF, supported image and Office sources are listed. Choose PDF, Image sources or Office sources to match your document task. Video and audio inputs remain available in their own tools.'
        return text
