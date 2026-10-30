"""Lazy trim/split waveform. Only Qt painting and existing FFmpeg are required."""
from collections import OrderedDict

from PySide6.QtCore import Qt,QThread,Signal,QTimer,QPointF,QRectF
from PySide6.QtGui import QPainter,QColor,QPen
from PySide6.QtWidgets import QWidget,QFrame,QVBoxLayout,QHBoxLayout,QLabel,QComboBox,QPushButton,QLineEdit


def stamp(value):
    value=max(0,value)
    minutes,seconds=divmod(value,60)
    hours,minutes=divmod(int(minutes),60)
    return (f'{hours:02}:' if hours else '')+f'{minutes:02}:{seconds:06.3f}'


class WaveWorker(QThread):
    ready=Signal(object,object)
    failed=Signal(str)
    def __init__(self,key,record,track,parent):
        super().__init__(parent)
        self.key,self.record,self.track=key,record,track
    def run(self):
        try:
            from .waveform_data import extract
            result=extract(self.record,self.track,self.isInterruptionRequested)
            if not self.isInterruptionRequested():self.ready.emit(self.key,result)
        except Exception as error:
            if not self.isInterruptionRequested():self.failed.emit(str(error))


class WaveCanvas(QWidget):
    edited=Signal(float,float)
    def __init__(self,parent=None):
        super().__init__(parent)
        self.data=None
        self.ranges=[]
        self.selected=0
        self.drag=None
        self.handle='start'
        self.setMinimumHeight(175)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName('Audio waveform trim range')
        self.setToolTip('Drag either range edge. Left/Right adjusts the selected edge by 0.01 s; Shift uses 1 s. S/E selects the start/end edge. Exact times are editable below the waveform.')
    def x(self,seconds):return 14+seconds/self.data['duration']*max(1,self.width()-28)
    def seconds(self,x):return max(0,min(self.data['duration'],(x-14)/max(1,self.width()-28)*self.data['duration']))
    def paintEvent(self,event):
        painter=QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette=self.palette()
        rect=QRectF(14,12,self.width()-28,self.height()-48)
        painter.fillRect(rect,palette.base())
        painter.setPen(palette.mid().color());painter.drawRect(rect)
        if not self.data:return
        duration=self.data['duration']
        accent=palette.link().color()
        for index,(start,end) in enumerate(self.ranges):
            tint=QColor(accent);tint.setAlpha(48 if index==self.selected else 22)
            painter.fillRect(QRectF(self.x(start),rect.top(),self.x(end)-self.x(start),rect.height()),tint)
        peaks=self.data['peaks']
        for pixel in range(max(1,int(rect.width()))):
            sample=min(len(peaks)-1,int(pixel/max(1,rect.width())*len(peaks)))
            time=duration*pixel/max(1,rect.width())
            inside=any(a<=time<=b for a,b in self.ranges)
            painter.setPen(QPen(accent if inside else palette.placeholderText().color(),1))
            height=max(1,peaks[sample]*rect.height()*.46)
            x=rect.left()+pixel
            painter.drawLine(QPointF(x,rect.center().y()-height),QPointF(x,rect.center().y()+height))
        if self.ranges:
            for value in self.ranges[self.selected]:
                painter.setPen(QPen(accent,2));painter.drawLine(QPointF(self.x(value),rect.top()),QPointF(self.x(value),rect.bottom()))
                painter.setBrush(palette.base());painter.drawEllipse(QPointF(self.x(value),rect.top()+5),5,5)
        painter.setPen(palette.placeholderText().color())
        ticks=min(5,max(2,int(rect.width()/80)))
        for n in range(ticks+1):
            time=duration*n/ticks
            label=stamp(time).rstrip('0').rstrip('.')
            width=painter.fontMetrics().horizontalAdvance(label)+4
            x=max(0,min(self.width()-width,self.x(time)-width/2))
            painter.drawText(QRectF(x,rect.bottom()+9,width,22),Qt.AlignmentFlag.AlignCenter,label)
    def mousePressEvent(self,event):
        if not self.isEnabled() or not self.data or not self.ranges:return
        value=self.seconds(event.position().x())
        self.selected=min(range(len(self.ranges)),key=lambda n:min(abs(value-self.ranges[n][0]),abs(value-self.ranges[n][1])))
        start,end=self.ranges[self.selected]
        self.drag='start' if abs(value-start)<abs(value-end) else 'end'
        self.handle=self.drag
        self.setFocus()
        self.move_handle(value)
    def mouseMoveEvent(self,event):
        if self.drag and self.isEnabled():self.move_handle(self.seconds(event.position().x()))
    def mouseReleaseEvent(self,event):self.drag=None
    def move_handle(self,value):
        start,end=self.ranges[self.selected]
        low=self.ranges[self.selected-1][1] if self.selected else 0
        high=self.ranges[self.selected+1][0] if self.selected+1<len(self.ranges) else self.data['duration']
        if (self.drag or self.handle)=='start':start=max(low,min(end-.001,value))
        else:end=min(high,max(start+.001,value))
        self.ranges[self.selected]=(start,end)
        self.edited.emit(start,end)
        self.update()
    def keyPressEvent(self,event):
        if not self.ranges or not self.isEnabled():return super().keyPressEvent(event)
        if event.key() in (Qt.Key.Key_S,Qt.Key.Key_E):
            self.handle='start' if event.key()==Qt.Key.Key_S else 'end';return
        if event.key() in (Qt.Key.Key_Left,Qt.Key.Key_Right):
            delta=(1 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else .01)*(1 if event.key()==Qt.Key.Key_Right else -1)
            self.move_handle(self.ranges[self.selected][self.handle=='end']+delta)
            return
        super().keyPressEvent(event)


class WaveformPreview(QFrame):
    def __init__(self,host):
        super().__init__()
        self.host=host
        self.worker=None
        self.cache=OrderedDict()
        self.key=None
        self.record=None
        self.loading=False
        self.closing=False
        layout=QVBoxLayout(self)
        layout.setContentsMargins(8,8,8,8)
        row=QHBoxLayout()
        self.file=QLabel('')
        self.file.setTextFormat(Qt.TextFormat.PlainText)
        self.file.setWordWrap(True)
        self.file.setMaximumHeight(40)
        row.addWidget(self.file,1)
        self.track=QComboBox()
        self.track.setAccessibleName('Waveform preview track')
        self.track.setToolTip('Preview track only. Processing tracks are selected in Audio settings.')
        self.track.currentIndexChanged.connect(self.schedule)
        row.addWidget(self.track)
        self.reload=QPushButton('Refresh waveform')
        self.reload.clicked.connect(self.regenerate)
        row.addWidget(self.reload)
        layout.addLayout(row)
        self.canvas=WaveCanvas()
        self.canvas.edited.connect(self.apply_range)
        layout.addWidget(self.canvas,1)
        selection=QHBoxLayout()
        self.selection_fields=[]
        for title in ('Start','End'):
            selection.addWidget(QLabel(title))
            control=QLineEdit()
            control.setAccessibleName('Waveform '+title.lower())
            control.setToolTip('Seconds, MM:SS or HH:MM:SS. Applies to the highlighted range.')
            control.editingFinished.connect(self.edit_selection)
            selection.addWidget(control,1)
            self.selection_fields.append(control)
        from .help_ui import InfoButton
        selection.addWidget(InfoButton(self.canvas.toolTip(),name='Waveform selection help'))
        layout.addLayout(selection)
        self.message=QLabel('')
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.message)
        self.timer=QTimer(host)
        self.timer.setSingleShot(True)
        self.timer.setInterval(180)
        self.timer.timeout.connect(self.refresh)
        host.table.selectionModel().selectionChanged.connect(self.schedule)
        host.model.modelReset.connect(self.schedule)
        for control in (host.audio_controls.task,host.audio_controls.tracks):control.currentIndexChanged.connect(self.schedule)
        for control in (host.audio_controls.start,host.audio_controls.end):control.textChanged.connect(self.update_range)
        host.audio_controls.ranges.textChanged.connect(self.update_range)
        self.hide()
    def eligible(self):return self.host.tool=='Audio' and self.host.audio_controls.task.currentData() in ('trim','split')
    def sync_enabled(self):
        enabled=bool(self.canvas.data and self.canvas.ranges) and not self.host.busy
        self.canvas.setEnabled(enabled)
        self.canvas.setCursor(Qt.CursorShape.PointingHandCursor if enabled else Qt.CursorShape.ArrowCursor)
        for control in self.selection_fields:control.setEnabled(enabled)
    def schedule(self,*args):
        if not self.closing and self.eligible() and self.host.tabs.currentIndex()==1:self.timer.start()
    def cancel(self):
        self.timer.stop()
        if self.worker:self.worker.requestInterruption()
    def regenerate(self):
        self.cache.clear();self.schedule()
    def refresh(self):
        host=self.host
        if self.closing or host._closing or not self.eligible() or host.tabs.currentIndex()!=1:
            self.hide();self.cancel();return
        self.show()
        rows=host.table.selectionModel().selectedRows()
        record=host.model.items[host.proxy.mapToSource(rows[0]).row()] if rows else next((r for r in host.model.items if host.proxy.spec.matches(r)),None)
        if record is None or record.sample:
            self.message.setText('Select an audio file.');self.canvas.data=None;self.canvas.update();self.sync_enabled();return
        self.file.setText(record.name)
        self.file.setToolTip(record.path)
        track=max(0,self.track.currentIndex())
        if self.record is None or self.record.identity_key!=record.identity_key or self.record.path!=record.path:
            track=0
            chosen=host.audio_controls.chosen.split(',')[0]
            if host.audio_controls.tracks.currentData()=='custom' and chosen.strip().isdigit():track=max(0,int(chosen)-1)
            self.track.blockSignals(True);self.track.clear();self.track.blockSignals(False)
        self.record=record
        key=(record.path,record.device,record.inode,record.size,record.modified_ns,track)
        self.key=key
        if key in self.cache:
            from .jobs import check_source
            try:check_source(dict(source=record.path,identity=[record.device,record.inode,record.size,record.modified_ns]))
            except (ValueError,OSError) as error:
                self.cache.pop(key,None)
                self.canvas.data=None;self.canvas.update();self.sync_enabled();self.message.setText(str(error));return
            self.present(key,self.cache[key]);return
        queue=host.jobs_dialog
        if host.busy or queue and queue.enabled:
            self.message.setText('Waveform waits for the active operation.')
            self.timer.start(500)
            return
        if self.worker:
            if self.worker.key!=key:self.worker.requestInterruption()
            return
        self.canvas.data=None;self.canvas.update()
        self.sync_enabled()
        self.message.setText('Generating waveform…')
        self.worker=WaveWorker(key,record,track,host)
        self.worker.ready.connect(self.present)
        self.worker.failed.connect(self.message.setText)
        self.worker.finished.connect(self.finished)
        self.worker.start()
    def finished(self):
        worker,self.worker=self.worker,None
        worker.deleteLater()
        if not self.closing and (worker.key!=self.key or worker.isInterruptionRequested()):self.schedule()
    def present(self,key,result):
        self.cache[key]=result
        self.cache.move_to_end(key)
        while len(self.cache)>3:self.cache.popitem(last=False)
        if key!=self.key or not self.eligible() or self.host.tabs.currentIndex()!=1:return
        self.track.blockSignals(True)
        self.track.clear()
        for n,track in enumerate(result['tracks']):self.track.addItem(f"Track {n+1} · {track['codec']}")
        self.track.setCurrentIndex(result['track'])
        self.track.blockSignals(False)
        self.canvas.data=result
        self.update_range()
    def update_range(self,*args):
        if not self.canvas.data:return
        from .audio_edit import parse_ranges
        from .planning import PlanRequest
        controls=self.host.audio_controls
        duration=self.canvas.data['duration']
        try:
            ranges=parse_ranges(PlanRequest('audio',audio_action=controls.task.currentData(),audio_start=controls.start.text(),
                audio_end=controls.end.text(),audio_ranges=controls.ranges.toPlainText()))
            ranges=[(start,duration if end is None else end) for start,end in ranges]
            if any(not 0<=start<end<=duration+.001 for start,end in ranges):raise ValueError('Range exceeds this track duration.')
            self.canvas.ranges=ranges
            self.canvas.selected=min(self.canvas.selected,len(ranges)-1)
            start,end=ranges[self.canvas.selected]
            for control,value in zip(self.selection_fields,(start,end)):
                if not control.hasFocus():control.setText(stamp(value))
            self.message.setText(f'Keep {stamp(start)} → {stamp(end)} · Duration {stamp(end-start)}')
            self.canvas.setAccessibleDescription(self.message.text())
        except ValueError as error:
            self.canvas.ranges=[]
            self.message.setText(str(error))
        self.canvas.update()
        self.sync_enabled()
    def apply_range(self,start,end):
        controls=self.host.audio_controls
        if self.host.busy:return
        if controls.task.currentData()=='trim':
            controls.start.setText(stamp(start));controls.end.setText(stamp(end))
        else:
            ranges=list(self.canvas.ranges)
            ranges[self.canvas.selected]=(start,end)
            controls.ranges.setPlainText('\n'.join(f'{stamp(a)} - {stamp(b)}' for a,b in ranges))
    def edit_selection(self):
        if self.host.busy or not self.canvas.data:return
        from .audio_edit import seconds
        try:
            start,end=[seconds(control.text()) for control in self.selection_fields]
            ranges=self.canvas.ranges
            n=self.canvas.selected
            low=ranges[n-1][1] if n else 0
            high=ranges[n+1][0] if n+1<len(ranges) else self.canvas.data['duration']
            if not low<=start<end<=high:raise ValueError('Range must fit this track without overlapping another range.')
            self.apply_range(start,end)
        except (ValueError,TypeError,IndexError) as error:self.message.setText(str(error))
    def shutdown(self):
        self.closing=True;self.cancel()
        return self.worker is None
