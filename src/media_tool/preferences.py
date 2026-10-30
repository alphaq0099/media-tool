"""Bounded, allowlisted UI defaults. No source lists, credentials or job snapshots."""
import json
import math

from PySide6.QtCore import QObject,QTimer,QSignalBlocker,Qt
from PySide6.QtWidgets import QComboBox,QCheckBox,QLineEdit,QDoubleSpinBox


FIELDS={
    'Images':{'':'format image_quality alpha_background image_metadata keep collision',
        'resize_controls':'mode width height percent aspect no_upscale'},
    'Video':{'':'format detail video_speed video_threads video_audio_mode video_subtitle_mode video_omissions keep collision',
        'video_format_controls':'action codec','resize_controls':'mode width height percent aspect no_upscale',
        'compression_controls':'mode amount unit audio reuse'},
    'Audio':{'':'format collision','audio_controls':'task mode tracks rate_mode quality bitrate sample_rate channels fade_in fade_out gain lufs true_peak tag_mode cover_mode'},
    'Rename':{'':'format detail start_number separator collision'},
    'Archives':{'':'format collision','archive_controls':'mode level number_pages encrypt_names hidden hidden_folders empty exclusions'},
    'Documents':{'':'format collision','document_controls':'task split_mode rotation dpi quality paper margin compression downsample image_dpi metadata_mode protection language'},
}


def read(widget):
    if isinstance(widget,QComboBox):return widget.currentText()
    if isinstance(widget,QCheckBox):return widget.isChecked()
    if isinstance(widget,QLineEdit):return widget.text()[:512]
    return widget.value()


def write(widget,value):
    if isinstance(widget,QComboBox):
        index=widget.findText(value) if isinstance(value,str) else -1
        if index>=0 and widget.model().index(index,0).flags() & Qt.ItemFlag.ItemIsEnabled:widget.setCurrentIndex(index)
    elif isinstance(widget,QCheckBox):
        if isinstance(value,bool):widget.setChecked(value)
    elif isinstance(widget,QLineEdit):
        if isinstance(value,str):widget.setText(value[:512])
    elif isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value):
        value=max(widget.minimum(),min(widget.maximum(),value))
        widget.setValue(value if isinstance(widget,QDoubleSpinBox) else int(value))


class ToolPreferences(QObject):
    def __init__(self,host):
        super().__init__(host)
        self.host=host
        self.loading=False
        self.bound=set()
        self.data={}
        self.tool=None
        self.mode=None
        self.last={}
        raw=host.settings.value('defaults/v1','{}')
        try:
            if isinstance(raw,str) and len(raw)<65536:
                parsed=json.loads(raw)
                if isinstance(parsed,dict):self.data=parsed
        except (ValueError,TypeError):pass
        self.timer=QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(350)
        self.timer.timeout.connect(self.flush)

    def widgets(self,tool=None):
        result={}
        for owner_name,names in FIELDS.get(tool or self.host.tool,{}).items():
            owner=getattr(self.host,owner_name,None) if owner_name else self.host
            if owner:
                for name in names.split():
                    widget=getattr(owner,name)
                    result['.'.join(filter(None,(owner_name,name)))]=widget
        return result

    def capture(self):return {key:read(widget) for key,widget in self.widgets().items()}

    def mode_key(self):
        host=self.host
        if host.tool=='Audio':return host.audio_controls.task.currentData()
        if host.tool=='Documents':return host.document_controls.task.currentData()
        if host.tool=='Video':return host.video_format_controls.action.currentData()
        return 'default'

    def bind(self):
        for widget in self.widgets().values():
            if widget in self.bound:continue
            self.bound.add(widget)
            signal=(widget.currentIndexChanged if isinstance(widget,QComboBox) else
                widget.toggled if isinstance(widget,QCheckBox) else widget.textChanged if isinstance(widget,QLineEdit) else widget.valueChanged)
            signal.connect(self.changed)

    def entry(self):
        tool=self.host.tool
        if not isinstance(self.data.get(tool),dict):self.data[tool]={}
        return self.data[tool]

    def apply(self,values):
        if not isinstance(values,dict) or not values:return
        from .workflow_ui import restore_ui
        widgets=self.widgets()
        valid={key:value for key,value in values.items() if key in widgets and isinstance(value,(str,int,float,bool))}
        self.loading=True
        try:
            # Prepare task/container-dependent choices before restoring their values.
            selectors=('document_controls.task','audio_controls.task','video_format_controls.action')
            for key in selectors:
                if key in valid:
                    with QSignalBlocker(widgets[key]):write(widgets[key],valid[key])
            if self.host.tool=='Documents':self.host.document_controls.update_visibility()
            if 'format' in valid:
                with QSignalBlocker(widgets['format']):write(widgets['format'],valid['format'])
            if self.host.tool=='Video':self.host.video_format_controls.update_visibility()
            # Use the existing visibility/format synchronization after setting values.
            for key,value in valid.items():
                with QSignalBlocker(widgets[key]):write(widgets[key],value)
            restore_ui(self.host,{'_chosen_audio_tracks':self.host.audio_controls.chosen})
        finally:self.loading=False

    def enter(self):
        self.tool=self.host.tool
        self.bind()
        if not self.host.editing_workflow():self.apply(self.entry().get('last',{}))
        self.mode=self.mode_key()
        self.last=self.capture()

    def save_current(self):
        if self.loading or self.tool!=self.host.tool or self.host.editing_workflow():return
        self.last=self.capture()
        entry=self.entry()
        entry['last']=self.last
        if not isinstance(entry.get('modes'),dict):entry['modes']={}
        entry['modes'][self.mode_key()]=self.last
        self.timer.start()

    def changed(self,*args):
        host=self.host
        if self.loading or self.tool!=host.tool or not host._planning_ready or host.visual.switching or host.editing_workflow():return
        mode=self.mode_key()
        if mode!=self.mode:
            entry=self.entry()
            if not isinstance(entry.get('modes'),dict):entry['modes']={}
            modes=entry['modes']
            if self.mode and self.last:modes[self.mode]=self.last
            self.apply(modes.get(mode,{}))
            self.mode=mode
        self.save_current()

    def flush(self):
        self.timer.stop()
        self.host.settings.setValue('defaults/v1',json.dumps(self.data,ensure_ascii=True))
        self.host.settings.sync()

    def install_folder(self,page):
        controls={name:getattr(page,name) for name in ('operation','minimum','maximum','depth','directories','scope','collision','loose','remove_empty')}
        values=self.data.get('Folders',{})
        if isinstance(values,dict):
            for name,widget in controls.items():
                with QSignalBlocker(widget):
                    if name in values:write(widget,values[name])
        page.mode_changed()
        def save(*args):
            self.data['Folders']={key:read(widget) for key,widget in controls.items()}
            self.timer.start()
        for widget in controls.values():
            signal=widget.currentIndexChanged if isinstance(widget,QComboBox) else widget.toggled if isinstance(widget,QCheckBox) else widget.valueChanged
            signal.connect(save)
