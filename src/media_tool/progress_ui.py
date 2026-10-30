"""Tool-scoped Results and transient activity. Poll only while a queue worker runs."""
from collections import Counter
import json

from PySide6.QtCore import QTimer,Qt
from PySide6.QtWidgets import QFrame,QWidget,QVBoxLayout,QHBoxLayout,QLabel,QPushButton
from .help_ui import InfoButton

TOOLS={'images':'Images','video':'Video','audio':'Audio','rename':'Rename','archives':'Archives','documents':'Documents','flatten':'Folders'}


def clock(seconds):
    seconds=max(0,int(seconds or 0))
    hours,seconds=divmod(seconds,3600)
    minutes,seconds=divmod(seconds,60)
    return f'{hours:02}:{minutes:02}:{seconds:02}' if hours else f'{minutes:02}:{seconds:02}'


class ResultsActivity:
    def __init__(self,host):
        self.host=host
        self.bindings={}
        self.events={}
        self.percent={}
        self.current_job=None
        self.snapshot_key=None
        self.snapshot_items=[]
        self.snapshot_rows=[]
        self.timer=QTimer(host)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.tick)
        self.page=QWidget()
        layout=QVBoxLayout(self.page)
        layout.setContentsMargins(0,10,0,0)
        self.panel=QFrame()
        self.panel.setObjectName('activityPanel')
        card=QVBoxLayout(self.panel)
        card.setContentsMargins(16,12,16,12)
        header=QHBoxLayout()
        self.title=QLabel('Results')
        self.title.setObjectName('activityTitle')
        self.title.setWordWrap(True)
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setMaximumHeight(60)
        self.value=QLabel('')
        header.addWidget(self.title,1)
        header.addWidget(self.value)
        header.addWidget(InfoButton(self.information,name='Progress and selected result information'))
        card.addLayout(header)
        host.main_workspace.layout().removeWidget(host.progress)
        card.addWidget(host.progress)
        self.detail=QLabel('')
        self.detail.setWordWrap(True)
        self.detail.setTextFormat(Qt.TextFormat.PlainText)
        self.detail.setMaximumHeight(60)
        card.addWidget(self.detail)
        self.counts=QLabel('')
        self.counts.setObjectName('muted')
        self.counts.setWordWrap(True)
        card.addWidget(self.counts)
        self.cancel=QPushButton('Cancel operation')
        self.cancel.clicked.connect(lambda:host.cancel_scan(background=True))
        card.addWidget(self.cancel)
        layout.addWidget(self.panel)
        host.tabs.removeTab(2)
        layout.addWidget(host.results_table,1)
        host.results_table.show()
        host.tabs.addTab(self.page,'Results')
        self.panel.hide()

    def information(self):
        lines=[self.title.text()]
        if hasattr(self,'detail') and self.detail.text():lines.append(self.detail.text())
        selected=self.host.results_table.selectionModel().selectedRows()
        if selected and selected[0].row()<len(self.host.result_model.items):
            item=self.host.result_model.items[selected[0].row()]
            lines.extend(str(item[key]) for key in ('source','output','details') if item.get(key))
        return '\n\n'.join(dict.fromkeys(lines))

    def active(self):
        queue=self.host.jobs_dialog
        if queue is None or queue.worker is None:return None
        worker=queue.worker
        if worker.adapter!='workflow':
            return dict(tool=TOOLS.get(worker.adapter),job_id=worker.job_id,parent_id=worker.job_id,adapter=worker.adapter)
        for item in queue.store.items(worker.job_id):
            if item['state'] in ('running','verifying','committing'):
                spec=json.loads(item['spec'])
                return dict(tool=TOOLS.get(spec['request']['operation']),
                    job_id=json.loads(item['stage'] or '{}').get('child_job'),parent_id=worker.job_id,
                    adapter=spec['request']['operation'],step=item['ordinal']+1,steps=len(queue.store.items(worker.job_id)))
        return None

    def job_started(self,job):
        host=self.host
        binding=dict(tool=TOOLS.get(job['adapter']),job_id=job['id'],parent_id=job['id'],adapter=job['adapter'])
        if binding['tool']:self.bindings[binding['tool']]=binding
        self.current_job=job['id']
        if hasattr(host,'waveform'):host.waveform.cancel()
        if binding['tool']==host.tool:
            host.tabs.setCurrentIndex(2)
        if binding['tool']=='Folders' and host.folders_page is not None:
            host.folders_page.tabs.setCurrentIndex(2)
        self.timer.start()
        self.tick()

    def activity_event(self,event):
        previous=self.events.get(event['item_id'],{})
        if event['state']=='media':previous.update(event)
        else:
            # Media timestamps belong only to the active encode phase.
            previous=dict(event)
        self.events[event['item_id']]=previous
        while len(self.events)>64:self.events.pop(next(iter(self.events)))

    def percent_event(self,item,done,total):
        self.percent[item]=(done,total)
        while len(self.percent)>64:self.percent.pop(next(iter(self.percent)))

    def enter(self,tool):
        active=self.active()
        if active and active['tool']==tool:
            self.bindings[tool]=active
        self.render(tool)
        self.host.tabs.setCurrentIndex(2 if active and active['tool']==tool else 0)

    def job_finished(self,worker):
        self.timer.stop()
        if self.bindings.get(self.host.tool,{}).get('parent_id')==worker.job_id:self.host.status.clear()
        self.tick()
        # A finished workflow's latest child remains bound to its own tool.
        self.render(self.host.tool)
        self.events.clear()
        self.percent.clear()
        self.current_job=None

    def remember(self,tool,job_id,adapter):
        if job_id:self.bindings[tool]=dict(tool=tool,job_id=job_id,parent_id=job_id,adapter=adapter)

    def tick(self):
        host=self.host
        queue=host.jobs_dialog
        if queue is None or queue.closed:return
        try:
            binding=self.active()
            if binding and binding['tool']:
                self.bindings[binding['tool']]=binding
                if binding['tool']=='Folders' and host.folders_page is not None:
                    self.render_folder(binding)
            self.render(host.tool)
        except Exception as error:
            self.detail.setText(str(error))
        if queue.worker is None:self.timer.stop()

    def render_folder(self,binding):
        page=self.host.folders_page
        if not binding['job_id']:return
        items=self.host.jobs_dialog.store.items(binding['job_id'])
        active=next((i for i in items if i['state'] in ('running','verifying','committing')),None)
        page.result_job=binding['job_id']
        page.results_model.replace([i['id'] for i in items],[
            (json.loads(i['spec'])['source'],json.loads(i['spec']).get('output') or '—',i['state'],i['message']) for i in items])
        if active:
            page.activity_title.setText(active['message']+' · '+json.loads(active['spec'])['name'])
        done=sum(i['state'] in ('succeeded','skipped') for i in items)
        page.activity_counts.setText(f'{done}/{len(items)} moves completed')
        page.progress.setRange(0,100)
        page.progress.setValue(round(100*done/max(len(items),1)))

    def render(self,tool):
        host=self.host
        queue=host.jobs_dialog
        binding=self.bindings.get(tool)
        if queue is None or queue.closed or binding is None:
            self.panel.hide()
            host.result_can_undo=False
            if host.last_result_job is not None:
                host.result_model.set_items(())
                host.last_result_job=None
            return
        job_id=binding['job_id']
        job=queue.store.db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone() if job_id else None
        if job_id and job is None:
            self.panel.hide();host.result_model.set_items(());host.last_result_job=None;return
        snapshot_key=(job_id,queue.store.db.total_changes,queue.store.db.execute('PRAGMA data_version').fetchone()[0])
        if snapshot_key!=self.snapshot_key:
            items=queue.store.items(job_id) if job_id else []
            rows=[]
            for item in items:
                spec=json.loads(item['spec'])
                rows.append(dict(source=spec.get('source',spec['name']),output=spec.get('output') or '',state=item['state'],
                    details=item['message'],source_label=spec['name'] if binding['adapter'] in ('audio','archives','documents') else None))
            self.snapshot_key=snapshot_key
            self.snapshot_items,self.snapshot_rows=items,rows
        items,rows=self.snapshot_items,self.snapshot_rows
        active=next((i for i in items if i['state'] in ('running','verifying','committing')),None)
        running=self.active()
        running=bool(running and running['tool']==tool and running['parent_id']==binding['parent_id'])
        if tuple(rows)!=host.result_model.items:
            selected=host.results_table.currentIndex().row()
            host.result_model.set_items(tuple(rows))
            if 0<=selected<len(rows):host.results_table.selectRow(selected)
        host.last_result_job=job_id
        for adapter in ('images','video','audio','archives','documents'):
            setattr(host,'result_is_'+adapter,binding['adapter']==adapter)
        host.result_can_undo=bool(job_id and binding['adapter']=='rename' and not queue.store.original_job(job_id)
            and not queue.store.undo_job(job_id) and any(i['state']=='succeeded' for i in items)
            and all(i['state'] in ('succeeded','skipped') for i in items))
        self.panel.show()
        counts=Counter(i['state'] for i in items)
        event=self.events.get(active['id'],{}) if active else {}
        if active:
            spec=json.loads(active['spec'])
            if active['state']=='verifying':stage='Verifying output'
            elif active['state']=='committing':stage='Saving output'
            else:stage=event.get('message') or active['message'] or 'Preparing'
            cancelling=bool(queue.worker and queue.worker.cancelled.is_set())
            self.title.setText(('Cancelling' if cancelling else stage)+' · '+spec['name'])
            self.title.setToolTip(self.title.text())
            done,total=self.percent.get(active['id'],(0,0))
            if not total and binding['parent_id']!=job_id:
                for step in queue.store.items(binding['parent_id']):
                    if step['state']=='running':done,total=self.percent.get(step['id'],(0,0));break
            host.progress.setRange(0,100 if total else 0)
            host.progress.setValue(done)
            self.value.setText(f'File {done}%' if total else '')
            text=f"File {active['ordinal']+1} of {len(items)}"
            if event.get('duration') and event.get('processed') is not None:
                text+=f" · Processed {clock(min(event['processed'],event['duration']))} / {clock(event['duration'])}"
        else:
            state=job['state'] if job else 'running'
            self.title.setText('Inspecting workflow inputs' if running and not items else state.capitalize())
            complete=counts['succeeded']+counts['skipped']
            host.progress.setRange(0,0 if running and not items else 100)
            value=round(100*complete/max(len(items),1))
            host.progress.setValue(value)
            self.value.setText(f'Batch {value}%' if items else '')
            failed=next((i for i in items if i['state'] in ('failed','interrupted','cancelled')),None)
            text=failed['message'] if failed else ''
        if binding.get('step'):text=f"Step {binding['step']}/{binding['steps']} · "+text
        seconds=queue.store.elapsed_times(live=True,job_id=binding['parent_id']).get(binding['parent_id'])
        if seconds is not None:text+=(' · ' if text else '')+'Elapsed '+clock(seconds)
        self.detail.setText(text)
        self.detail.setToolTip(text)
        self.counts.setText(' · '.join(f'{number} {label}' for number,label in (
            (counts['succeeded'],'completed'),(sum(counts[s] for s in ('running','verifying','committing')),'processing'),
            (counts['queued'],'waiting'),(counts['skipped'],'skipped'),(counts['failed']+counts['interrupted'],'failed'),(counts['cancelled'],'cancelled')) if number))
        self.cancel.setVisible(running)
        self.cancel.setEnabled(running and not (queue.worker and queue.worker.cancelled.is_set()))
        host._update_undo_controls()
