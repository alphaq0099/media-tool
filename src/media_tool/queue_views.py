"""Current operations in one file table; older results live in History."""
import json
from pathlib import Path

from PySide6.QtCore import Qt,QSignalBlocker
from PySide6.QtWidgets import QWidget,QFrame,QVBoxLayout,QHBoxLayout,QLineEdit,QLabel,QComboBox,QHeaderView

from .flow_layout import FlowLayout
from .help_ui import InfoButton


class QueueViews:
    def __init__(self,queue,history,history_layout,actions):
        from .jobs_ui import Rows,table
        self.queue=queue
        self.records={}
        self.page=QFrame()
        self.page.setObjectName('panel')
        layout=QVBoxLayout(self.page)
        layout.setContentsMargins(12,12,12,12)
        layout.setSpacing(10)
        filters=FlowLayout()
        self.search=QLineEdit()
        self.search.setPlaceholderText('Search jobs or files…')
        self.search.setAccessibleName('Search current jobs and files')
        self.search.setProperty('flowWidth',200)
        filters.addWidget(self.search,1)
        self.state=QComboBox()
        for title,key in (('All current jobs','all'),('Waiting','queued'),('Running','running'),('Paused','paused')):self.state.addItem(title,key)
        self.state.setAccessibleName('Current job status filter')
        filters.addWidget(self.state)
        filters.addWidget(InfoButton(lambda:'Jobs shows waiting and active operations. Finished results and retry/undo actions are in History. Start queue follows the saved order across filters. Actions apply to the selected job.'+
            ('\n\n'+queue.detail.text() if queue.detail.text() else ''),name='Queue and selected file information'))
        layout.addLayout(filters)
        self.model=Rows(('Job / operation','File name','Status','Attempts','Details'),queue)
        self.table=table(self.model)
        self.table.setAccessibleName('Current job files')
        header=self.table.horizontalHeader()
        for column,width in ((0,175),(2,100),(3,80)):
            header.setSectionResizeMode(column,QHeaderView.ResizeMode.Interactive)
            self.table.setColumnWidth(column,width)
        for column in (1,4):header.setSectionResizeMode(column,QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table,1)
        self.count=QLabel('0 files')
        self.count.setObjectName('muted')
        layout.addWidget(self.count)
        queue.tabs.setTabText(queue.tabs.indexOf(history),'History')
        queue.tabs.insertTab(0,self.page,'Jobs')
        index=queue.tabs.indexOf(queue.workflow_editor)
        queue.tabs.removeTab(index)
        queue.tabs.insertTab(1,queue.workflow_editor,'Workflow')
        self.history=history
        # Shared transport controls stay in the same footer for Jobs/History.
        self.footer=QWidget()
        footer=QVBoxLayout(self.footer)
        footer.setContentsMargins(0,0,0,0)
        footer.setSpacing(8)
        for control in (queue.progress,queue.step_progress,queue.progress_detail,queue.status):
            hidden=control.isHidden()
            history_layout.removeWidget(control)
            footer.addWidget(control)
            control.setVisible(not hidden)
        history_layout.removeItem(actions)
        actions.setParent(None)
        footer.addLayout(actions)
        queue.layout().addWidget(self.footer)
        self.search.textChanged.connect(lambda:self.refresh())
        self.state.currentIndexChanged.connect(lambda:self.refresh())
        self.table.selectionModel().selectionChanged.connect(self.selection_changed)
        queue.tabs.currentChanged.connect(self.tab_changed)
        queue.tabs.setCurrentIndex(0)

    def selected(self):
        rows=self.table.selectionModel().selectedRows()
        return self.records.get(self.model.keys[rows[0].row()]) if rows else None

    def current(self):return self.queue.tabs.currentWidget() is self.page

    def refresh(self,jobs=None,select=None):
        queue=self.queue
        jobs=queue.saved_jobs() if jobs is None else jobs
        if select and self.current() and any(job['id']==select and job['state'] not in ('queued','running','paused','interrupted','blocked') for job in jobs):
            queue.tabs.setCurrentWidget(self.history)
        old=self.selected()
        query=self.search.text().strip().casefold()
        state=self.state.currentData()
        keys,rows,records=[],[],{}
        # A single bulk query keeps large waiting queues out of an N+1 loop.
        items={}
        for item in queue.store.db.execute("""SELECT items.* FROM items JOIN jobs ON jobs.id=items.job_id
            WHERE jobs.state IN ('queued','running','paused','interrupted','blocked') ORDER BY items.ordinal"""):
            items.setdefault(item['job_id'],[]).append(dict(item))
        children={}
        for job in jobs:
            if job['state'] not in ('queued','running','paused','interrupted','blocked'):continue
            if state!='all' and job['state']!=state:continue
            for item in items.get(job['id'],[]):
                spec=json.loads(item['spec'])
                child=json.loads(item['stage'] or '{}').get('child_job') if job['adapter']=='workflow' else None
                if child not in children:children[child]=queue.store.items(child) if child else []
                sources=children[child] if child and children[child] else [item]
                for source in sources:
                    child_spec=json.loads(source['spec'])
                    name=Path(child_spec['source']).name if child_spec.get('source') else child_spec['name']
                    operation=job['title']+(f" · {spec['name']}" if job['adapter']=='workflow' else '')
                    details=source['message']
                    if job['adapter']=='workflow' and not child:
                        roots=spec.get('roots',[])
                        name=Path(roots[0]['source']).name if len(roots)==1 else f'{len(roots)} files' if roots else '—'
                        details=details or ('Waiting to inspect inputs' if roots else 'Waiting for previous step')
                    if query and query not in (operation+' '+name+' '+details).casefold():continue
                    key=source['id']
                    keys.append(key)
                    rows.append((operation,name,source['state'],str(source['attempts']),details))
                    records[key]=dict(job_id=job['id'],item_id=key)
        blocker=QSignalBlocker(self.table.selectionModel())
        self.model.replace(keys,rows)
        self.records=records
        target=next((key for key in keys if records[key]['job_id']==select),None) if select else None
        target=target or (old['item_id'] if old and old['item_id'] in records else None)
        if not target and keys:target=keys[0]
        if target:self.table.selectRow(keys.index(target))
        blocker.unblock()
        self.count.setText(f'{len(keys):,} file'+('' if len(keys)==1 else 's'))
        self.update_footer()

    def selection_changed(self,*args):
        self.queue.refresh_items()

    def update_footer(self):
        queue=self.queue
        active=bool(queue.worker or queue.enabled)
        visible=queue.tabs.currentWidget() is not queue.workflow_editor
        self.footer.setVisible(visible)
        if self.current():
            for widget in (queue.progress,queue.progress_detail):widget.setVisible(active)
            if not active:queue.step_progress.hide()
        else:
            queue.progress.show()
            queue.progress_detail.show()

    def tab_changed(self,*args):
        self.update_footer()
        self.queue.refresh_items()
