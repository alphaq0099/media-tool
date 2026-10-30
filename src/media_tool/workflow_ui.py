"""Embedded workflow drafts; ordinary tool panels supply step settings."""
from dataclasses import asdict,replace
import json
from pathlib import Path
import uuid

from PySide6.QtCore import Qt,QSignalBlocker,QThread,Signal
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QLabel,QLineEdit,QComboBox,
    QPushButton,QListWidget,QPlainTextEdit,QScrollArea,QFileDialog,QSpinBox,QDoubleSpinBox,QCheckBox,QSlider,QFrame,QSplitter,QMenu)

from .domain import FileRecord
from .flow_layout import FlowLayout
from .planning import PlanRequest
from .workflows import LABELS,workflow_snapshot


def record_data(record):
    return {key:getattr(record,key) for key in ('path','name','size','kind','sample','modified_ns','device','inode')}


def setting_widgets(host):
    owners={'':host,'video_format_controls':host.video_format_controls,'resize_controls':host.resize_controls,
        'compression_controls':host.compression_controls,'audio_controls':host.audio_controls,'archive_controls':host.archive_controls}
    if host.document_controls is not None:owners['document_controls']=host.document_controls
    supported=(QLineEdit,QComboBox,QSpinBox,QDoubleSpinBox,QCheckBox,QPlainTextEdit,QSlider)
    for prefix,owner in owners.items():
        for key,value in vars(owner).items():
            if not prefix and key in ('name_filter','extension_filter','category_filter','recursive'):continue
            if any(word in key.casefold() for word in ('password','confirm')):continue
            pairs=value.items() if isinstance(value,dict) else [(None,value)]
            for sub,widget in pairs:
                if isinstance(widget,supported):
                    yield '.'.join(filter(None,(prefix,key,str(sub) if sub is not None else ''))),widget


def capture_ui(host):
    result={}
    for name,widget in setting_widgets(host):
        if isinstance(widget,QLineEdit):value=widget.text()
        elif isinstance(widget,QComboBox):value=widget.currentText()
        elif isinstance(widget,(QSpinBox,QDoubleSpinBox,QSlider)):value=widget.value()
        elif isinstance(widget,QCheckBox):value=widget.isChecked()
        else:value=widget.toPlainText()
        result[name]=value
    result['_chosen_audio_tracks']=host.audio_controls.chosen
    return result


def restore_ui(host,state):
    widgets=list(setting_widgets(host))
    blockers=[QSignalBlocker(w) for _,w in widgets]
    for name,widget in widgets:
        if name not in state:continue
        value=state[name]
        if isinstance(widget,QLineEdit):widget.setText(value)
        elif isinstance(widget,QComboBox):widget.setCurrentText(value)
        elif isinstance(widget,(QSpinBox,QDoubleSpinBox,QSlider)):widget.setValue(value)
        elif isinstance(widget,QCheckBox):widget.setChecked(value)
        else:widget.setPlainText(value)
    del blockers
    host.audio_controls.chosen=state.get('_chosen_audio_tracks','')
    host._rename_controls()
    host.video_format_controls.update_visibility()
    host.resize_controls.set_tool(host.tool)
    host.audio_controls.changed()
    host.archive_controls.update_visibility()
    if host.document_controls is not None:host.document_controls.update_visibility()


def request_summary(data):
    r=PlanRequest(**data)
    resize='Original size' if r.resize_mode=='none' else f'Resize: {r.resize_percent}%' if r.resize_mode=='percent' else f'Resize: {r.resize_width} × {r.resize_height}'
    if r.operation=='video':
        if r.video_action=='remux':return f'Remux → {r.extension.upper()}; copy compatible tracks'
        codec={'h264':'H.264','hevc':'H.265','vp9':'VP9'}.get(r.video_codec,r.video_codec)
        speed={'medium':'Balanced','veryfast':'Fast','superfast':'Fastest'}.get(r.video_preset,r.video_preset)
        quality={28:'Low',23:'Balanced',20:'High',18:'Best'}.get(r.video_crf,str(r.video_crf))
        compression=f'Maximum {r.video_target_size:g} {r.video_target_unit}' if r.video_mode=='target_size' else 'Quality: '+quality
        return f'{r.extension.upper()} / {codec}; {compression}; Speed: {speed}; {resize}'
    if r.operation=='rename':
        name=f'{r.prefix}{r.start:0{r.padding}d}…' if r.rename_style=='sequence' else 'Random names' if r.rename_style=='random' else 'Clean separators'
        return f'{name}; keep extension'
    if r.operation=='archives':
        level={0:'Store (fastest)',1:'Moderated (fast)',6:'Balanced (normal)',9:'Maximum (slowest)'}.get(r.archive_level,str(r.archive_level))
        kind='7z' if r.archive_format=='7z' else r.archive_format.upper()
        return f'{kind}; '+('one archive: '+r.archive_name if r.archive_mode=='combined' else 'one archive per file')+f'; {level}'+('; password protected' if r.archive_encrypt else '')
    if r.operation=='audio':
        action={'convert':'Convert / extract','normalize':'Normalize loudness','volume':'Volume','tags':'Tags & cover'}.get(r.audio_action,r.audio_action.title())
        return f'{action} → {r.extension.upper()}; '+('Copy encoded audio' if r.audio_mode=='copy' else 'Convert audio')
    if r.operation=='documents':
        compression=('; JPEG images, quality '+str(r.document_quality) if r.document_compression=='jpeg' else '; lossless cleanup') if r.document_task=='optimize' else ''
        return f'{r.document_task.replace("_"," ").title()} → {r.extension.upper()}; pages {r.document_pages or "all"}'+compression
    return f'{"JPEG" if r.extension=="jpg" else r.extension.upper()}; {resize}; Quality: {r.image_quality}'


class WorkflowPlanWorker(QThread):
    ready=Signal(object)
    failed=Signal(str)
    def __init__(self,records,steps,parent):
        super().__init__(parent)
        self.records,self.steps=tuple(records),json.loads(json.dumps(steps))
    def run(self):
        try:
            specs=workflow_snapshot(self.records,self.steps,self.isInterruptionRequested)
            if not self.isInterruptionRequested():self.ready.emit(specs)
        except Exception as error:
            if not self.isInterruptionRequested():self.failed.emit(str(error))


class WorkflowEditor(QWidget):
    def __init__(self,queue):
        super().__init__(queue)
        self.queue,self.host=queue,queue.host
        self.setObjectName('workspace')
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground,True)
        self.inputs,self.steps=[],[]
        self.active=False
        self.edit_index=None
        outer=QVBoxLayout(self)
        outer.setContentsMargins(0,0,0,0)
        scroll=QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body=QWidget()
        self.layout=QVBoxLayout(body)
        self.layout.setContentsMargins(8,8,8,8)
        outer.addWidget(scroll)
        scroll.setWidget(body)
        self.name=QLineEdit('My workflow')
        self.name.setMaxLength(120)
        self.name.setAccessibleName('Workflow name')
        header=FlowLayout()
        header.addWidget(QLabel('Name'))
        header.addWidget(self.name,1)
        self.recipe=QComboBox()
        self.recipe.setAccessibleName('Saved workflow recipe')
        self.recipe.setPlaceholderText('Saved recipes')
        self.recipe.setMinimumWidth(140)
        header.addWidget(self.recipe)
        recipes=QPushButton('Recipes')
        menu=QMenu(recipes)
        for title,callback in (('New workflow',self.new),('Save recipe',self.save_recipe),('Load recipe',self.load_recipe),('Delete recipe',self.delete_recipe)):
            menu.addAction(title,callback)
        recipes.setMenu(menu)
        header.addWidget(recipes)
        self.layout.addLayout(header)
        self.row([('Use workspace files',self.use_workspace),('Add files…',self.add_files),('Clear inputs',self.clear_inputs)])
        self.input_list=QListWidget()
        self.input_list.setAccessibleName('Workflow input files')
        self.input_list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.input_list.setMinimumHeight(48)
        self.input_list.setMaximumHeight(64)
        self.layout.addWidget(self.input_list)
        from .help_ui import InfoButton
        steps_panel=QFrame()
        steps_panel.setObjectName('panel')
        step_layout=QVBoxLayout(steps_panel)
        step_layout.setContentsMargins(12,12,12,12)
        steps_heading=QHBoxLayout()
        steps_heading.addWidget(QLabel('Steps'),1)
        steps_heading.addWidget(InfoButton('Steps run in order. Each receives the previous step’s verified outputs.'))
        step_layout.addLayout(steps_heading)
        self.step_list=QListWidget()
        self.step_list.setAccessibleName('Planned workflow steps')
        self.step_list.setObjectName('workflowSteps')
        self.step_list.setWordWrap(True)
        self.step_list.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.step_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.step_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.step_list.setMinimumHeight(150)
        self.step_list.currentRowChanged.connect(self.show_step)
        step_layout.addWidget(self.step_list,1)
        choose=FlowLayout()
        self.tool=QComboBox()
        for label in ('Images','Video','Audio','Rename','Archives','Documents'):self.tool.addItem(label)
        choose.addWidget(self.tool)
        self.configure_button=QPushButton('Configure step')
        self.configure_button.clicked.connect(self.configure)
        choose.addWidget(self.configure_button)
        self.step_buttons={}
        for title,callback in (('Edit',self.edit),('↑',lambda:self.move(-1)),('↓',lambda:self.move(1)),('Remove',self.remove)):
            button=QPushButton(title)
            button.setAccessibleName({'↑':'Move step up','↓':'Move step down'}.get(title,title+' step'))
            button.clicked.connect(callback)
            if title in ('↑','↓'):button.setFixedWidth(34)
            self.step_buttons[title]=button
            choose.addWidget(button)
        step_layout.insertLayout(1,choose)
        summary_panel=QFrame()
        summary_panel.setObjectName('panel')
        summary_layout=QVBoxLayout(summary_panel)
        summary_layout.setContentsMargins(12,12,12,12)
        summary_heading=QHBoxLayout()
        summary_heading.addWidget(QLabel('Workflow preview'),1)
        summary_heading.addWidget(InfoButton('The diagram follows all steps in order. Select a step to highlight it; Edit opens its tool settings. Filenames are estimates: collisions, random names, tracks, pages and compatibility are checked against actual outputs when each step begins. No files are created by this preview.'))
        summary_layout.addLayout(summary_heading)
        from .workflow_preview import WorkflowPreview
        self.preview=WorkflowPreview()
        self.preview.setMinimumHeight(150)
        summary_layout.addWidget(self.preview,1)
        self.flow=QLabel()
        self.flow.setWordWrap(True)
        self.flow.setTextFormat(Qt.TextFormat.PlainText)
        summary_layout.addWidget(self.flow)
        splitter=QSplitter()
        splitter.addWidget(steps_panel)
        splitter.addWidget(summary_panel)
        splitter.setStretchFactor(0,1)
        splitter.setStretchFactor(1,1)
        self.splitter=splitter
        self.layout.addWidget(splitter,1)
        self.status=QLabel('')
        self.status.setToolTip('Configure a tool, then use Add step to workflow above its settings.')
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.hide()
        self.footer=QWidget()
        footer=QVBoxLayout(self.footer)
        footer.setContentsMargins(8,8,8,10)
        footer.setSpacing(8)
        footer.addWidget(self.status)
        actions=FlowLayout()
        self.enqueue_button=QPushButton('Add workflow to Queue')
        self.start_button=QPushButton('Start workflow')
        for button in (self.enqueue_button,self.start_button):actions.addWidget(button)
        self.enqueue_button.clicked.connect(lambda:self.submit(False))
        self.start_button.clicked.connect(lambda:self.submit(True))
        footer.addLayout(actions)
        outer.addWidget(self.footer)
        self.layout.addStretch()
        try:
            draft=json.loads(str(self.host.settings.value('workflow/draft','{}')))
            self.inputs=draft.get('inputs',[])
            self.steps=draft.get('steps',[])
            self.active=draft.get('active',False)
            self.name.setText(draft.get('name','My workflow'))
        except (ValueError,TypeError):pass
        self.name.textChanged.connect(self.save_draft)
        self.refresh()
        self.refresh_recipes()

    def row(self,choices):
        layout=FlowLayout()
        for title,callback in choices:
            button=QPushButton(title)
            button.clicked.connect(callback)
            layout.addWidget(button)
        self.layout.addLayout(layout)

    def message(self,value):
        self.status.setText(str(value))
        self.status.setVisible(bool(value))

    def records(self):return tuple(FileRecord(**data) for data in self.inputs)

    def workspace_records(self):
        host=self.host
        host.apply_filters()
        rows=host.table.selectionModel().selectedRows()
        records=([host.model.items[host.proxy.mapToSource(row).row()] for row in rows] if rows else
                 [r for r in host.model.items if host.proxy.spec.matches(r)])
        if any(r.sample or r.kind=='Folder' for r in records):raise ValueError('Choose real files for the workflow.')
        return records

    def new(self):
        self.inputs,self.steps=[],[]
        self.active=True
        self.edit_index=None
        self.name.setText('My workflow')
        self.refresh()
        self.message('')

    def leave(self):
        self.active=False
        self.edit_index=None
        self.save_draft()
        if hasattr(self.host,'preferences'):self.host.preferences.enter()
        self.host.update_controls()
        self.host.status.setText('Workflow draft saved. Tools now run normally.')

    def use_workspace(self):
        try:
            records=self.workspace_records()
            if not records:raise ValueError('Add files in a tool first, or use Add files here.')
            self.inputs=[record_data(r) for r in records]
            self.active=True
            self.refresh()
        except Exception as error:self.message(error)

    def add_files(self):
        paths,_=QFileDialog.getOpenFileNames(self,'Workflow input files')
        try:
            by_path={r['path']:r for r in self.inputs}
            for path in paths:
                record=FileRecord.from_path(path)
                by_path[record.path]=record_data(record)
            self.inputs=list(by_path.values())
            self.active=True
            self.refresh()
        except Exception as error:self.message(error)

    def clear_inputs(self):
        self.inputs=[]
        self.refresh()

    def configure(self):
        self.active=True
        self.edit_index=None
        title=self.tool.currentText()
        if self.host.tool==title:
            self.host.workspace_stack.setCurrentWidget(self.host.main_workspace)
            for control in self.host.nav_group.buttons():control.setChecked(control.text()==title)
        else:self.host._tool_changed(title)
        self.update_banner()
        self.save_draft()

    def edit(self):
        index=self.step_list.currentRow()
        if index<0:return
        step=self.steps[index]
        self.edit_index=index
        self.active=True
        tool={'archives':'Archives','rename':'Rename'}.get(step['request']['operation'],LABELS[step['request']['operation']])
        self.host._tool_changed(tool)
        restore_ui(self.host,step.get('ui_state',{}))
        if tool=='Archives':
            from .seven_archive import password_for
            try:value=password_for(dict(encrypted=step['request']['archive_encrypt'],password_token=step.get('archive_secret'))) or ''
            except ValueError:value=''
            self.host.archive_controls.password.setText(value)
            self.host.archive_controls.confirm_password.setText(value)
        if tool=='Documents':
            from .private_values import resolve
            for key,control in (('document_input_secret',self.host.document_controls.input_password),
                                ('document_output_secret',self.host.document_controls.output_password)):
                try:value=resolve(step['request'].get(key))
                except ValueError:value=''
                control.setText(value)
            self.host.document_controls.confirm.setText(self.host.document_controls.output_password.text())
        self.update_banner()

    def capture(self):
        try:
            if self.host.editing_busy:raise ValueError('Wait for the current operation to finish before changing a workflow.')
            if self.edit_index is None and len(self.steps)>=20:raise ValueError('A workflow supports at most 20 steps.')
            request=replace(self.host.plan_request(),keep_originals=True,collision='unique')
            if request.operation!='archives':request=replace(request,archive_encrypt=False)
            if not self.steps and request.operation=='rename':raise ValueError('Add a conversion or archive step before Rename.')
            if self.edit_index is not None and self.steps[self.edit_index]['request']['operation']!=request.operation:
                raise ValueError('Use the original tool to edit this step, or choose Configure step to append another tool.')
            secret=''
            if request.operation=='archives' and request.archive_encrypt:
                secret=self.host.archive_controls.authorize(({'encrypted':True},))[0]['password_token']
            if self.edit_index is not None and self.steps[self.edit_index]['request'].get('archive_encrypt') and not request.archive_encrypt:
                raise ValueError('Re-enter the archive password to edit this encrypted step, or remove it and add an unencrypted step.')
            if request.operation=='documents':
                if request.document_protection=='set' and not request.document_output_secret:
                    raise ValueError('Enter the new PDF password before adding or updating this step.')
                if self.edit_index is not None and self.steps[self.edit_index]['request'].get('document_input_secret') and not request.document_input_secret:
                    raise ValueError('Re-enter the input PDF password before updating this protected step.')
            step=dict(label=self.host.tool,request=asdict(request),archive_secret=secret,ui_state=capture_ui(self.host))
            selected=len(self.steps) if self.edit_index is None else self.edit_index
            if self.edit_index is None:self.steps.append(step)
            else:self.steps[self.edit_index]=step
            self.edit_index=None
            if not self.inputs:self.inputs=[record_data(r) for r in self.workspace_records()]
            self.active=True
            self.refresh()
            self.step_list.setCurrentRow(selected)
            self.queue.show()
            self.queue.tabs.setCurrentWidget(self)
            self.message('Step saved.')
        except Exception as error:self.host.status.setText(str(error))

    def move(self,delta):
        index=self.step_list.currentRow()
        target=index+delta
        if 0<=index<len(self.steps) and 0<=target<len(self.steps):
            self.steps[index],self.steps[target]=self.steps[target],self.steps[index]
            self.edit_index=None
            self.refresh()
            self.step_list.setCurrentRow(target)

    def remove(self):
        index=self.step_list.currentRow()
        if index>=0:
            self.steps.pop(index)
            self.edit_index=None
            self.refresh()

    def show_step(self,index):
        if hasattr(self,'preview'):self.preview.select(index)
        for title,button in self.step_buttons.items():
            valid=0<=index<len(self.steps)
            button.setEnabled(valid and (index>0 if title=='↑' else index<len(self.steps)-1 if title=='↓' else True))

    def refresh(self):
        selected=self.step_list.currentRow()
        self.input_list.clear()
        self.input_list.addItems([f'{r["kind"]}: {r["name"]} ({r["path"]})' for r in self.inputs])
        self.input_list.setVisible(bool(self.inputs))
        with QSignalBlocker(self.step_list):
            self.step_list.clear()
            self.step_list.addItems([f'{index+1}. {step["label"]}: [{request_summary(step["request"]).replace("; ",", ")}]' for index,step in enumerate(self.steps)])
            if self.steps:self.step_list.setCurrentRow(max(0,min(selected,len(self.steps)-1)))
        self.preview.render(self.inputs,self.steps)
        self.show_step(self.step_list.currentRow())
        self.flow.setText(f'{len(self.inputs)} input file(s) → '+(' → '.join(s['label'] for s in self.steps) or 'No steps yet'))
        self.flow.setToolTip('Originals and generated outputs are kept.')
        self.update_banner()
        self.save_draft()

    def save_draft(self,*args):
        self.host.settings.setValue('workflow/draft',json.dumps(dict(inputs=self.inputs,steps=self.steps,active=self.active,name=self.name.text())))
        self.update_banner()

    def update_banner(self):
        if not hasattr(self.host,'workflow_bar'):return
        self.host.workflow_bar.setVisible(self.active)
        self.host.workflow_label.setText(f'Workflow: {self.name.text()} • {len(self.steps)} step(s) • Originals kept')
        self.host.workflow_capture_button.setText('Update workflow step' if self.edit_index is not None else 'Add step to workflow')

    def recipes(self):
        try:return json.loads(str(self.host.settings.value('workflow/recipes','{}')))
        except (ValueError,TypeError):return {}

    def refresh_recipes(self):
        self.recipe.clear()
        self.recipe.addItems(sorted(self.recipes()))

    def save_recipe(self):
        if not self.steps:
            self.message('Add steps before saving a recipe.')
            return
        steps=json.loads(json.dumps(self.steps))
        for step in steps:
            step['archive_secret']=''
            for key in ('document_input_secret','document_output_secret'):
                step['request'][key]='required' if step['request'].get(key) else ''
        recipes=self.recipes()
        recipes[self.name.text().strip() or 'My workflow']=steps
        self.host.settings.setValue('workflow/recipes',json.dumps(recipes))
        self.refresh_recipes()
        self.message('Recipe saved. Inputs are chosen separately; passwords are not saved.')

    def load_recipe(self):
        name=self.recipe.currentText()
        recipes=self.recipes()
        if name not in recipes:return
        self.steps=json.loads(json.dumps(recipes[name]))
        for step in self.steps:
            if step['request'].get('archive_encrypt'):step['archive_secret']=uuid.uuid4().hex
            for key in ('document_input_secret','document_output_secret'):
                if step['request'].get(key):step['request'][key]=uuid.uuid4().hex
        self.active=True
        self.edit_index=None
        self.name.setText(name)
        self.refresh()
        self.message('Recipe loaded. Choose inputs; protected steps ask for passwords when started.')

    def delete_recipe(self):
        recipes=self.recipes()
        recipes.pop(self.recipe.currentText(),None)
        self.host.settings.setValue('workflow/recipes',json.dumps(recipes))
        self.refresh_recipes()

    def submit(self,start):
        try:
            if self.queue.plan_worker or self.host.editing_busy:raise ValueError('Wait for current work before validating another workflow.')
            if start and not self.queue.can_start_immediately:raise ValueError('Queue is active. Add the workflow to Queue instead.')
            self.pending_start=start
            self.pending_title=self.name.text().strip() or 'Workflow'
            worker=WorkflowPlanWorker(self.records(),self.steps,self.queue)
            self.queue.plan_worker=worker
            worker.ready.connect(self.planned)
            worker.failed.connect(self.message)
            worker.finished.connect(self.finished)
            self.setEnabled(False)
            self.message('Checking workflow inputs…')
            worker.start()
        except Exception as error:self.message(error)

    def planned(self,specs):
        if self.host._closing:return
        try:
            job=self.queue.store.enqueue(self.pending_title,'workflow',specs)
            self.queue.refresh(job)
            self.queue.tabs.setCurrentIndex(0)
            self.queue.status.setText('Workflow saved.')
            self.pending_job=job
        except Exception as error:self.message(error)

    def finished(self):
        worker,self.queue.plan_worker=self.queue.plan_worker,None
        worker.deleteLater()
        self.setEnabled(True)
        job=getattr(self,'pending_job',None)
        self.pending_job=None
        self.queue.refresh(job)
        if job and self.pending_start and not self.host._closing:self.queue.start_single(job)
        self.host.update_controls()


def require_passwords(queue,job_id):
    from PySide6.QtWidgets import QInputDialog
    from .private_values import resolve,remember
    from .seven_archive import password_for,remember_password
    entries={}
    for item in queue.store.items(job_id):
        spec=json.loads(item['spec'])
        if item['state']=='succeeded':continue
        for key,title in (('document_input_secret','Input PDF password'),('document_output_secret','Output PDF password')):
            token=spec['request'].get(key)
            if token:entries[('pdf',token)]=title
        if spec.get('archive_secret'):entries[('archive',spec['archive_secret'])]='Archive password'
    for (kind,token),title in entries.items():
        try:
            resolve(token) if kind=='pdf' else password_for(dict(encrypted=True,password_token=token))
        except ValueError:
            value,accepted=QInputDialog.getText(queue,title,'Enter the password for this workflow:',QLineEdit.EchoMode.Password)
            if not accepted:return False
            remember(value,token) if kind=='pdf' else remember_password(value,token)
    return True
