"""Graphite workspace composition; existing tool controls/engines remain owners."""
from PySide6.QtCore import Qt,QEvent,QObject
from PySide6.QtWidgets import QFrame,QWidget,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QLayout,QSizePolicy

from .help_ui import InfoButton,InfoHoverOnly
from .progress_ui import ResultsActivity


class InspectorGroup(QFrame):
    def __init__(self,title,explanation='',collapsed=False):
        super().__init__()
        self.setObjectName('inspectorGroup')
        self.setSizePolicy(QSizePolicy.Policy.Preferred,QSizePolicy.Policy.Maximum)
        root=QVBoxLayout(self)
        root.setVerticalSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        root.setContentsMargins(12,4,12,10)
        root.setSpacing(4)
        root.setAlignment(Qt.AlignmentFlag.AlignTop)
        row=QHBoxLayout()
        self.header=QPushButton(title)
        self.header.setObjectName('groupHeader')
        self.header.setCheckable(True)
        self.header.setChecked(not collapsed)
        row.addWidget(self.header,1)
        self.explanation=explanation
        self.help=InfoButton(lambda:self.information(),name=title+' information')
        row.addWidget(self.help)
        root.addLayout(row)
        self.body=QWidget()
        self.body.setSizePolicy(QSizePolicy.Policy.Preferred,QSizePolicy.Policy.Maximum)
        self.content=QVBoxLayout(self.body)
        self.content.setVerticalSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.content.setContentsMargins(0,0,0,0)
        self.content.setSpacing(7)
        self.content.setAlignment(Qt.AlignmentFlag.AlignTop)
        root.addWidget(self.body)
        self.body.setVisible(not collapsed)
        self.header.toggled.connect(self.body.setVisible)
        self.header.toggled.connect(lambda checked:self.set_title(self.title))
        self.set_title(title)

    def set_title(self,title):
        self.title=title
        self.header.setText(title.upper()+('  ▾' if self.header.isChecked() else '  ▸'))

    def information(self):
        base=self.explanation() if callable(self.explanation) else self.explanation
        lines=[base] if base else []
        if hasattr(self,'body'):
            for widget in self.body.findChildren(QWidget):
                tip=widget.toolTip()
                if tip and not isinstance(widget,InfoButton) and widget.isVisibleTo(self.body) and tip not in lines:
                    lines.append(tip)
        return '\n\n'.join(lines)


class VisualWorkspace(QObject):
    def __init__(self,host):
        super().__init__(host)
        self.host=host
        self.help_policy=InfoHoverOnly(host)
        self.switching=False
        self.refreshing=False
        self.initialized=False
        self.states={}
        self.items=[]
        self.groups={}
        self.cells=[]
        self.activity=ResultsActivity(host)
        self.activity_event=self.activity.activity_event
        self.percent_event=self.activity.percent_event
        self.job_started=self.activity.job_started
        self.job_finished=self.activity.job_finished
        host.subtitle.hide()
        host.subtitle.setToolTip(host.subtitle.text())
        self.heading_help=InfoButton(lambda:'\n\n'.join((host.subtitle.text(),host.add_button.toolTip(),
            host.open_folder_action.toolTip() if host.tool=='Archives' else host.recursive.toolTip(),
            host.input_types.information() if hasattr(host,'input_types') else '')),name='Tool information')
        headline=host.main_workspace.layout().itemAt(0).layout()
        headline.addWidget(self.heading_help,0,Qt.AlignmentFlag.AlignTop)
        host.settings_scroll.setFixedWidth(320)
        host.settings_panel.setMinimumWidth(250)
        host.settings_layout.setContentsMargins(0,0,0,0)
        host.settings_layout.setSpacing(10)
        host.settings_layout.setVerticalSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        host.main_workspace.layout().setContentsMargins(18,16,18,14)
        host.columns.setSpacing(14)
        host.file_layout.setContentsMargins(14,10,14,12)
        host.file_layout.setSpacing(8)
        host.add_button.setText('+ Open')
        for action in (host.open_files_action,host.open_folder_action):action.setParent(host)
        host.add_button.setMenu(None)
        host.add_button.setPopupMode(host.add_button.ToolButtonPopupMode.DelayedPopup)
        host.add_button.setObjectName('openButton')
        host.add_button.setStyleSheet('QToolButton#openButton {background: palette(link); color: palette(link-visited); font-weight: 600; padding: 9px 13px;}')
        host.name_filter.setPlaceholderText('Search files…')
        host.name_filter.setProperty('flowWidth',200)
        host.extension_filter.setMaximumWidth(150)
        self.filters=QPushButton('Filters')
        self.filters.setCheckable(True)
        self.filters.setToolTip('Extension filter and reset. Active filters also apply to Preview and Start.')
        self.filters.toggled.connect(lambda checked:self.tab_changed(host.tabs.currentIndex()))
        host.filter_row.addWidget(self.filters)
        host.name_filter.setProperty('flowWidth',140)
        host.category_filter.setProperty('flowWidth',145)
        host.preview_button.setText('Preview')
        self._inspector_actions()
        self._pair_primary_fields()
        self._compact_audio()
        self._group_items()
        host.main_workspace.installEventFilter(self)
        for table in (host.table,host.preview_table,host.results_table):
            table.setAlternatingRowColors(False)
            table.verticalHeader().setDefaultSectionSize(44)
        # Preview combines the ordinary frozen output plan with an optional
        # waveform. The original model/table and tab indices remain unchanged.
        from .waveform_ui import WaveformPreview
        host.waveform=WaveformPreview(host)
        preview=QWidget()
        layout=QVBoxLayout(preview)
        layout.setContentsMargins(0,8,0,0)
        layout.addWidget(host.waveform,2)
        host.tabs.removeTab(1)
        layout.addWidget(host.preview_table,1)
        host.preview_table.show()
        host.tabs.insertTab(1,preview,'Preview')
        self._stable_files_page()
        host.tabs.setCurrentIndex(0)
        self.wave_page=preview

    def _stable_files_page(self):
        host=self.host
        page=QWidget()
        layout=QVBoxLayout(page)
        layout.setContentsMargins(0,8,0,0)
        layout.setSpacing(8)
        for row in (host.file_toolbar,host.filter_row):
            host.file_layout.removeItem(row)
            row.setParent(None)
            layout.addLayout(row)
        host.tabs.removeTab(0)
        layout.addWidget(host.table,1)
        host.table.show()
        host.tabs.insertTab(0,page,'Files')
        self.files_page=page

    def eventFilter(self,watched,event):
        if event.type()==QEvent.Type.Resize:
            width=watched.width()
            self.inspector.setFixedWidth(320 if width>=960 else 280 if width>=760 else 250)
            from PySide6.QtWidgets import QBoxLayout
            self.track_pair.layout().setDirection(QBoxLayout.Direction.TopToBottom if self.inspector.width()<300 else QBoxLayout.Direction.LeftToRight)
        return super().eventFilter(watched,event)

    def _inspector_actions(self):
        host=self.host
        self.inspector=QWidget()
        column=QVBoxLayout(self.inspector)
        column.setContentsMargins(0,0,0,0)
        column.setSpacing(10)
        host.columns.removeWidget(host.settings_scroll)
        host.settings_scroll.setMinimumWidth(0)
        host.settings_scroll.setMaximumWidth(16777215)
        host.settings_panel.setMaximumWidth(16777215)
        column.addWidget(host.settings_scroll,1)
        host.actions.removeWidget(host.start_rename_button)
        column.addWidget(host.start_rename_button)
        host.start_rename_button.setMinimumHeight(42)
        row=QHBoxLayout()
        row.setSpacing(8)
        for control in (host.run_button,host.preview_button):
            host.actions.removeWidget(control)
            row.addWidget(control,1)
        column.addLayout(row)
        host.columns.addWidget(self.inspector)
        self.inspector.setFixedWidth(320)
        host.actions.removeWidget(host.issues_button)
        host.file_totals.addWidget(host.issues_button)

    def _detach(self,widget):
        hidden=widget.isHidden()
        for layout in self.host.settings_panel.findChildren(QVBoxLayout):layout.removeWidget(widget)
        self.host.settings_layout.removeWidget(widget)
        return hidden

    def _pair(self,fields):
        wrapper=QWidget()
        row=QHBoxLayout(wrapper)
        row.setContentsMargins(0,0,0,0)
        row.setSpacing(10)
        for title,control,condition in fields:
            label_hidden=self._detach(title)
            control_hidden=self._detach(control)
            cell=QWidget()
            column=QVBoxLayout(cell)
            column.setContentsMargins(0,0,0,0)
            column.setSpacing(5)
            column.setAlignment(Qt.AlignmentFlag.AlignTop)
            column.addWidget(title)
            column.addWidget(control)
            title.setVisible(not label_hidden)
            control.setVisible(not control_hidden)
            row.addWidget(cell,1)
            self.cells.append((cell,control,condition))
        return wrapper

    def _pair_primary_fields(self):
        host=self.host
        layout=host.settings_layout
        index=layout.indexOf(host.option_title)
        self.format_pair=self._pair([(host.option_title,host.format,lambda:True),
            (host.video_format_controls.codec_label,host.video_format_controls.codec,
                lambda:host.tool=='Video' and host.video_format_controls.action.currentData()!='remux')])
        layout.insertWidget(index,self.format_pair)
        index=layout.indexOf(host.detail_title)
        self.quality_pair=self._pair([(host.detail_title,host.detail,lambda:host.tool in ('Video','Rename')),
            (host.video_speed_label,host.video_speed,lambda:host.tool=='Video' and host.video_format_controls.action.currentData()!='remux')])
        layout.insertWidget(index,self.quality_pair)
        threads_label=next(label for label in host.video_panel.findChildren(QLabel) if label.text()=='CPU threads')
        self.cpu_pair=self._pair([(threads_label,host.video_threads,lambda:host.tool=='Video')])
        layout.insertWidget(layout.indexOf(host.keep),self.cpu_pair)
        host.video_panel.layout().removeWidget(host.video_omissions)
        layout.insertWidget(layout.indexOf(host.keep),host.video_omissions)
        host.video_format_controls.note.hide()
        audio_label=next(label for label in host.video_panel.findChildren(QLabel) if label.text()=='Audio tracks')
        subtitle_label=next(label for label in host.video_panel.findChildren(QLabel) if label.text()=='Existing subtitles')
        subtitle_label.setText('Subtitles')
        self.track_pair=self._pair([(audio_label,host.video_audio_mode,lambda:host.tool=='Video'),
            (subtitle_label,host.video_subtitle_mode,lambda:host.tool=='Video')])
        host.video_panel.layout().insertWidget(0,self.track_pair)

    def _compact_audio(self):
        audio=self.host.audio_controls
        layout=audio.panel.layout()
        self.audio_more=InspectorGroup('Advanced audio',
            lambda:'\n\n'.join((audio.sample_rate.toolTip(),audio.mode.toolTip(),audio.fade_in.toolTip())),collapsed=True)
        for title,control in (('Sample rate',audio.sample_rate),('Channels',audio.channels)):
            label=next(label for label in audio.panel.findChildren(QLabel) if label.text()==title)
            for widget in (label,control):
                layout.removeWidget(widget)
                self.audio_more.content.addWidget(widget)
        layout.removeWidget(audio.fade_panel)
        self.audio_more.content.addWidget(audio.fade_panel)
        layout.addWidget(self.audio_more)
        labels=audio.trim_panel.findChildren(QLabel)
        pair=self._pair([(labels[0],audio.start,lambda:self.host.tool=='Audio'),
            (labels[1],audio.end,lambda:self.host.tool=='Audio')])
        audio.trim_panel.layout().addWidget(pair)

    def _group_items(self):
        host=self.host
        root=host.settings_layout
        # New lazy document controls can be inserted into the root after setup.
        while root.count():
            item=root.takeAt(0)
            if item.widget() not in self.groups.values() and item.spacerItem() is None:self.items.append(item)
        if not self.groups:
            explanations={
                'Output':lambda:'Format and destination. Queued operations keep a frozen snapshot of these settings.'+ ('\n\n'+host.keep.toolTip() if host.tool in ('Images','Video') else ''),
                'Video':lambda:'Resize and image quality. PNG is lossless; JPEG and WebP quality control retained detail.' if host.tool=='Images' else 'Resize, retained detail and encoding speed. Quality and speed can be combined independently.',
                'Tracks':lambda:'\n\n'.join(filter(None,[host.video_audio_tracks.toolTip(),host.video_subtitle_mode.toolTip(),host.video_external_enabled.toolTip()])),
                'Naming':'Rename changes filenames while keeping extensions. Preview checks collisions before applying the batch.',
                'Audio':lambda:host.audio_controls.policy.text()+'\n'+host.audio_controls.policy.toolTip(),
                'Documents':lambda:host.document_controls.policy.text()+'\n'+host.document_controls.policy.toolTip() if host.document_controls else '',
                'Archives':'Archive mode, compression and protection. Blank password means no encryption. Originals are kept; review cleanup separately.',
                'Advanced':lambda:'\n\n'.join(filter(None,[host.collision.toolTip(),host.video_speed.toolTip(),host.image_metadata.toolTip(),host.keep.toolTip(),host.video_format_controls.note.text(),host.workflow_policy.text() if host.editing_workflow() else '']))}
            for name,explanation in explanations.items():self.groups[name]=InspectorGroup(name,explanation,collapsed=name=='Advanced')
        else:
            for group in self.groups.values():
                while group.content.count():group.content.takeAt(0)
        advanced={host.collision_label,host.collision,host.alpha_label,host.alpha_background,host.metadata_label,host.image_metadata,
            self.cpu_pair,host.video_omissions}
        naming={host.prefix_label,host.prefix,host.start_label,host.start_number,host.pattern_label,host.rename_pattern,host.separator_label,host.separator}
        videos={host.resize_controls.panel,host.compression_controls.panel,self.quality_pair,host.image_quality_label,host.image_quality}
        for item in self.items:
            widget=item.widget()
            group='Output'
            if widget in advanced:group='Output' if host.tool=='Archives' and widget in (host.collision_label,host.collision) else 'Advanced'
            elif widget in naming:group='Naming'
            elif widget in videos:group='Naming' if widget==self.quality_pair and host.tool=='Rename' else 'Video'
            elif widget==host.video_panel:group='Tracks'
            elif widget==host.audio_controls.panel:group='Audio'
            elif host.document_controls and widget==host.document_controls.panel:group='Documents'
            elif widget==host.archive_controls.panel:group='Archives'
            content=self.groups[group].content
            if widget is not None:
                hidden=widget.isHidden()
                content.addWidget(widget)
                widget.setVisible(not hidden)
            elif item.layout() is not None:
                item.layout().setParent(None)
                content.addLayout(item.layout())
        for group in self.groups.values():root.addWidget(group)
        root.addStretch()

    def before_tool_change(self):
        if hasattr(self.host,'preferences'):self.host.preferences.save_current()
        if self.initialized and not self.switching:
            from .workflow_ui import capture_ui
            self.states[self.host.tool]=capture_ui(self.host)

    def tool_changed(self):
        host=self.host
        self.switching=True
        try:
            if host.tool in self.states:
                from .workflow_ui import restore_ui
                restore_ui(host,self.states[host.tool])
            self.initialized=True
            if hasattr(host,'preferences'):host.preferences.enter()
            self._group_items()
            self.activity.enter(host.tool)
            self.refresh()
        finally:self.switching=False

    def tab_changed(self,index):
        host=self.host
        files=index==0
        for item in (host.file_toolbar,host.filter_row):
            for n in range(item.count()):
                widget=item.itemAt(n).widget()
                if widget:widget.setVisible(files and (widget!=host.recursive or host.tool!='Archives'))
        host.count_label.setVisible(files)
        host.extension_filter.setVisible(files and self.filters.isChecked())
        host.reset_filters_button.setVisible(files and (self.filters.isChecked() or bool(host.name_filter.text()) or host.category_filter.currentIndex()!=0 or bool(host.extension_filter.text())))
        host.waveform.setVisible(index==1 and host.waveform.eligible())
        if index==2:self.activity.render(host.tool)
        if index==1:host.waveform.schedule()
        else:host.waveform.cancel()
        self.refresh()

    def refresh(self):
        if self.refreshing:return
        self.refreshing=True
        try:
            host=self.host
            for cell,control,condition in self.cells:cell.setVisible(condition() and not control.isHidden())
            host.video_omissions.setVisible(host.tool=='Video')
            host.subtitle.hide()
            self.heading_help.update_tip()
            host.phase_hint.hide()
            host.workflow_policy.hide()
            host.original_cleanup_hint.hide()
            host.video_format_controls.note.hide()
            host.audio_controls.policy.hide()
            host.audio_controls.trim_panel.setVisible(host.tool=='Audio' and host.audio_controls.task.currentData()=='trim' and host.tabs.currentIndex()!=1)
            if hasattr(host,'waveform'):
                eligible=host.tabs.currentIndex()==1 and host.waveform.eligible()
                host.waveform.setVisible(eligible)
                host.waveform.sync_enabled()
                if not eligible:host.waveform.cancel()
            for label in host.video_external_panel.findChildren(QLabel):
                if len(label.text())>75:
                    host.video_external_enabled.setToolTip(label.text());label.hide()
            if host.document_controls:
                host.document_controls.policy.hide()
                host.document_controls.external_hint.hide()
                host.document_controls.external_check.setToolTip(host.document_controls.external_hint.text())
            if host.resize_controls.preview.text().startswith('Add '):host.resize_controls.preview.hide()
            for item in self.items:
                widget=item.widget()
                if isinstance(widget,QLabel) and widget.text()=='Output settings':widget.hide()
            allowed={'Output':True,'Video':host.tool in ('Video','Images'),'Tracks':host.tool=='Video',
                'Naming':host.tool=='Rename','Audio':host.tool=='Audio','Documents':host.tool=='Documents',
                'Archives':host.tool=='Archives','Advanced':host.tool in ('Video','Images','Rename')}
            for name,group in self.groups.items():group.setVisible(allowed[name])
            self.groups['Video'].set_title('Image' if host.tool=='Images' else 'Video')
            host.settings_panel.setEnabled(not host.editing_busy)
            host.issues_button.setVisible(bool(host.issues) and host.tabs.currentIndex()==0)
            host.remove_button.setVisible(host.tabs.currentIndex()==0 and bool(host.table.selectionModel().selectedRows()))
            host.preview_button.setText('Review workflow' if host.editing_workflow() else 'Preview')
            active=self.activity.active()
            host.cancel_button.setVisible(host.editing_busy)
            host.status.setVisible(bool(host.status.text()) and not (active and active['tool']==host.tool))
        finally:self.refreshing=False
