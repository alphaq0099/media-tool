"""Lightweight workflow diagram. Names are projections, never reserved outputs."""
from dataclasses import dataclass
from pathlib import Path
import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QWidget,QFrame,QScrollArea,QVBoxLayout,QHBoxLayout,
    QGridLayout,QLabel,QSizePolicy)

from .domain import natural_key
from .planning import PlanRequest
from .help_ui import InfoButton


@dataclass(frozen=True)
class Names:
    names: tuple | None = ()
    extension: str = ''
    pattern: str = ''

    def display(self):
        if self.names is None:return self.pattern
        if not self.names:return 'No inputs'
        text='\n'.join(self.names[:3])
        if len(self.names)>3:text+=f'\n+ {len(self.names)-3:,} more'
        return text


def projected_output(batch,request):
    """Pure naming sketch: no stat, probing, decoding, secrets or file writes."""
    r=request
    names=sorted(batch.names,key=natural_key) if batch.names is not None else None
    ext=r.extension
    if r.operation=='rename':
        if r.rename_style=='random':return Names(None,batch.extension,'Random names; extensions kept')
        if names is None:
            suffix='.'+batch.extension if batch.extension else ' (extension kept)'
            pattern=f'{r.prefix}{r.start:0{r.padding}d}{suffix}, …' if r.rename_style=='sequence' else 'Cleaned names; extensions kept'
            return Names(None,batch.extension,pattern)
        result=[]
        for index,name in enumerate(names):
            p=Path(name)
            stem=(f'{r.prefix}{r.start+index:0{r.padding}d}' if r.rename_style=='sequence' else
                re.sub(r'[\s_.-]+',r.separator,p.stem).strip(' _.-'))
            result.append(stem+p.suffix)
        return Names(tuple(result),batch.extension)
    if r.operation=='archives':
        ext=r.archive_format
        if r.archive_mode=='combined':
            name=r.archive_name.strip()
            return Names((name if name.casefold().endswith('.'+ext) else name+'.'+ext,),ext)
        if names is None:return Names(None,ext,f'One .{ext} archive per input file')
    if r.operation=='audio':
        if r.audio_action=='join':
            # A saved join order can change which input supplies the output stem.
            first=(Path(r.audio_join_order[0]).name if names and Path(r.audio_join_order[0]).name in names else None) if r.audio_join_order else names[0] if names else None
            return Names((Path(first).stem+'_joined.'+ext,),ext) if first else Names(None,ext,f'Joined audio (.{ext})')
        return Names(None,ext,{'split':f'Audio parts (.{ext}); names depend on tracks and ranges',
            'trim':f'Trimmed audio (.{ext}); names depend on selected tracks'}.get(r.audio_action,
            f'Audio files (.{ext}); one per selected track'))
    suffix=''
    if r.operation=='documents':
        task=r.document_task
        ext=ext if task in ('render','ocr') else 'txt' if task=='text' else 'pdf'
        if task in ('render','split'):
            return Names(None,ext,f'Page images (.{ext})' if task=='render' else f'Document parts (.{ext})')
        suffix='_'+task
        if task in ('merge','images_pdf') and names:
            first=Path(r.document_order[0]).name if r.document_order else names[0]
            if first not in names:return Names(None,ext,f'Combined document (.{ext}); order requires review')
            names=[first]
    if names is None:return Names(None,ext,f'Generated files (.{ext})')
    return Names(tuple(Path(name).stem+suffix+'.'+ext for name in names),ext)


def process_name(r):
    if r.operation=='images':return 'Resize and convert' if r.resize_mode!='none' else 'Format change'
    if r.operation=='video':
        return 'Remux' if r.video_action=='remux' else 'Compress video' if r.video_mode=='target_size' else 'Resize and convert' if r.resize_mode!='none' else 'Convert video'
    if r.operation=='rename':return 'Rename'
    if r.operation=='archives':return 'Archive'+(' · encrypted' if r.archive_encrypt else '')
    if r.operation=='audio':return {'convert':'Convert / extract audio','tags':'Edit tags / cover','normalize':'Normalize loudness'}.get(r.audio_action,r.audio_action.title()+' audio')
    return r.document_task.replace('_',' ').title()


def project(inputs,steps):
    batch=Names(tuple(r.get('name') or Path(r['path']).name for r in sorted(inputs,
        key=lambda r:(natural_key(r.get('name') or Path(r['path']).name),r['path']))))
    result=[]
    for step in steps:
        try:
            request=PlanRequest(**step['request'])
            output=projected_output(batch,request) if batch.names!=() else Names()
            result.append(dict(label=step['label'],input=batch.display(),process=process_name(request),output=output.display(),request=request))
            batch=output
        except (ValueError,TypeError,KeyError):
            result.append(dict(label=step.get('label','Step'),input=batch.display(),process='Review settings',output='Not available',request=None))
            batch=Names(None,'','Outputs require review')
    return result


class WorkflowPreview(QScrollArea):
    def __init__(self,parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setAccessibleName('Workflow input process output preview')
        self.body=QWidget()
        self.column=QVBoxLayout(self.body)
        self.column.setContentsMargins(0,0,4,0)
        self.column.setSpacing(7)
        self.setWidget(self.body)
        self.cards=[]
        self.rows=[]

    def render(self,inputs,steps):
        while self.column.count():
            item=self.column.takeAt(0)
            if item.widget():item.widget().hide();item.widget().deleteLater()
        self.cards=[]
        self.rows=project(inputs,steps)
        for index,data in enumerate(self.rows):
            if index:
                arrow=QLabel('↓')
                arrow.setObjectName('flowArrow')
                arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
                self.column.addWidget(arrow)
            card=QFrame()
            card.setObjectName('flowStep')
            card.setProperty('active',False)
            layout=QVBoxLayout(card)
            layout.setContentsMargins(10,8,10,10)
            header=QHBoxLayout()
            title=QLabel(f'{index+1}. {data["label"]}')
            title.setTextFormat(Qt.TextFormat.PlainText)
            title.setWordWrap(True)
            header.addWidget(title,1)
            from .workflow_ui import request_summary
            summary=request_summary(steps[index]['request']) if data['request'] else 'Review this step’s settings.'
            header.addWidget(InfoButton(summary+'\n\nNames are estimated. Actual outputs, tracks, pages and collisions are validated when this step runs. Destination: '+(data['request'].destination or 'input folder') if data['request'] else summary))
            layout.addLayout(header)
            grid=QGridLayout()
            grid.setHorizontalSpacing(9)
            grid.setVerticalSpacing(5)
            grid.setColumnStretch(1,1)
            for row,key in enumerate(('input','process','output')):
                caption=QLabel(key.title())
                caption.setObjectName('muted')
                grid.addWidget(caption,row,0,Qt.AlignmentFlag.AlignTop)
                value=QLabel(data[key])
                value.setTextFormat(Qt.TextFormat.PlainText)
                value.setWordWrap(True)
                value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                value.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred)
                grid.addWidget(value,row,1)
            layout.addLayout(grid)
            self.cards.append(card)
            self.column.addWidget(card)
        if not steps:
            empty=QLabel('No steps')
            empty.setObjectName('muted')
            self.column.addWidget(empty)
        self.column.addStretch()

    def select(self,index,scroll=True):
        for ordinal,card in enumerate(self.cards):
            active=ordinal==index
            if card.property('active')!=active:
                card.setProperty('active',active)
                card.style().unpolish(card)
                card.style().polish(card)
                card.update()
        if scroll and 0<=index<len(self.cards):self.ensureWidgetVisible(self.cards[index],0,10)
