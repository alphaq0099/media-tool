"""Lazy queue UI: opening the main workspace never opens the history database."""
from collections import Counter
import json
from pathlib import Path
import threading
import time

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSettings, QStandardPaths, Qt, QThread, QTimer, Signal, QUrl, QSignalBlocker
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout,
    QHeaderView, QLabel, QMessageBox, QProgressBar, QPushButton, QSplitter,
    QTableView, QVBoxLayout, QWidget, QTabWidget, QMenu, QLineEdit, QComboBox,QFrame,
)
from .jobs import Store, OwnerLock, copy_snapshot, execute_job


class Rows(QAbstractTableModel):
    def __init__(self, headers, parent=None):
        super().__init__(parent)
        self.headers = headers
        self.rows = []
        self.keys = []
        self.tips = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(self.headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.headers[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return self.rows[index.row()][index.column()]
        if role == Qt.ItemDataRole.ToolTipRole:
            return self.tips[index.row()]
        return None

    def replace(self, keys, rows, tips=None):
        tips = tips or [""] * len(rows)
        if (self.keys, self.rows, self.tips) != (keys, rows, tips):
            self.beginResetModel()
            self.keys, self.rows, self.tips = keys, rows, tips
            self.endResetModel()
            return True
        return False


def table(model):
    view = QTableView()
    view.setModel(model)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.setAlternatingRowColors(False)
    view.setShowGrid(False)
    view.verticalHeader().hide()
    view.verticalHeader().setDefaultSectionSize(40)
    view.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter)
    view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    view.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    return view


def text(value):
    label = QLabel(value)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


class JobWorker(QThread):
    progress = Signal(str, int, int)
    failed = Signal(str)
    renamed = Signal(object)
    activity = Signal(object)

    def __init__(self, directory, job_id, parent=None, adapter="copy"):
        super().__init__(parent)
        self.directory, self.job_id = directory, job_id
        self.adapter = adapter
        self.cancelled = threading.Event()
        self.paused = threading.Event()
        self.last_progress = 0
        self.started_at = None
        self.last_activity = (None, 0)

    def report_activity(self, value):
        key=(value['item_id'],value['state'],value['message'])
        current=time.monotonic()
        if key!=self.last_activity[0] or current-self.last_activity[1]>=.15:
            self.last_activity=(key,current)
            self.activity.emit(value)

    def report(self, item_id, done, total):
        current = time.monotonic()
        if current - self.last_progress >= 0.05 or done == total:
            self.last_progress = current
            # QThread signals use C++ int; report a percentage, not file byte counts.
            self.progress.emit(item_id, round(100 * done / max(total, 1)) if total else 0, 100 if total else 0)

    def run(self):
        from .activity import listener
        activity_token=listener.set(self.report_activity)
        store = None
        run_id = None
        try:
            store = Store(self.directory)
            self.started_at = time.monotonic()
            run_id = store.begin_run(self.job_id)
            execute_job(store, self.job_id, self.cancelled.is_set, self.paused.is_set, self.report)
        except Exception as error:
            self.failed.emit(f"Queue stopped: {error}. Resolve the error and restart the app to recover interrupted items.")
        finally:
            if store:
                try:
                    if run_id is not None:
                        store.end_run(run_id, time.monotonic() - self.started_at)
                    if self.adapter == "rename":
                        self._rename_updates(store)
                except Exception as error:
                    self.failed.emit(f"Could not refresh renamed files: {error}. Saved history was retained.")
                finally:
                    store.close()
            listener.reset(activity_token)

    def _rename_updates(self, store):
        from .domain import FileRecord
        updates = []
        for item in store.items(self.job_id):
            if item["state"] == "succeeded":
                spec = json.loads(item["spec"])
                try:
                    record = FileRecord.from_path(spec["output"])
                    if [record.device, record.inode, record.size, record.modified_ns] == spec["identity"]:
                        updates.append((spec, record))
                except OSError:
                    pass
        self.renamed.emit(updates)


class CopyPlanWorker(QThread):
    planned = Signal(object)
    failed = Signal(str)

    def __init__(self, records, destination, parent):
        super().__init__(parent)
        self.records = tuple(records)
        self.destination = destination

    def run(self):
        try:
            result = copy_snapshot(self.records, self.destination, self.isInterruptionRequested)
            if not self.isInterruptionRequested():
                self.planned.emit(result)
        except Exception as error:
            if not self.isInterruptionRequested():
                self.failed.emit(str(error))


class QueuePage(QWidget):
    def __init__(self, host, directory=None):
        super().__init__(host)
        self.host = host
        self.setWindowTitle("Queue")
        self.setObjectName('workspace')
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground,True)
        if directory is None:
            if host.settings.format() == QSettings.Format.IniFormat:
                directory = Path(host.settings.fileName()).parent / "jobs"
            else:
                directory = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)) / "jobs"
        self.directory = Path(directory)
        self.owner = OwnerLock(self.directory)
        try:
            self.store = Store(self.directory)
            recovered = self.store.recover()
        except BaseException:
            try:
                if hasattr(self, "store"):
                    self.store.close()
            finally:
                self.owner.close()
                self.deleteLater()
            raise
        self.worker = None
        self.plan_worker = None
        self.enabled = False
        self.single_job = None
        self.closed = False
        self.fatal = False
        self.data_version = -1
        self.live_progress = None
        self.run_baseline = 0
        self.run_elapsed_base = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18,16,18,14)
        title=text('Queue')
        title.setObjectName('heading')
        layout.addWidget(title)
        self.tabs=QTabWidget()
        layout.addWidget(self.tabs,1)
        history=QFrame()
        history.setObjectName('panel')
        layout=QVBoxLayout(history)
        layout.setContentsMargins(12,12,12,12)
        layout.setSpacing(10)
        self.tabs.addTab(history,'Jobs')
        from .workflow_ui import WorkflowEditor
        self.workflow_editor=WorkflowEditor(self)
        self.tabs.addTab(self.workflow_editor,'Plan workflow')
        from .flow_layout import FlowLayout
        add = FlowLayout()
        self.sample_button = QPushButton("Add sample job")
        self.failure_button = QPushButton("Add sample with a failure")
        self.copy_button = QPushButton("Copy current files…")
        self.undo_button = QPushButton("Preview undo")
        self.discard_analysis_button = QPushButton("Discard analysis")
        self.archive_results_button = QPushButton("Archive results")
        self.archive_results_button.clicked.connect(self.show_archive_results)
        self.discard_analysis_button.setToolTip("Remove owned two-pass logs for the selected paused video job to free disk space. Retry will analyze again.")
        self.discard_analysis_button.clicked.connect(self.discard_analysis)
        self.undo_button.clicked.connect(self.preview_selected_undo)
        diagnostics=QPushButton('Tools')
        menu=QMenu(diagnostics)
        self.copy_action=menu.addAction('Copy current files…',self.copy_current)
        menu.addSeparator()
        self.open_action=menu.addAction('Open output folder',self.open_output)
        self.export_action=menu.addAction('Save report…',self.export_report)
        self.support_action=menu.addAction('Export support report…',self.export_support_report)
        self.support_action.setToolTip('Share technical versions, operation settings and outcomes. Excludes filenames, paths, passwords, tags and free-form logs.')
        self.clear_history_action=menu.addAction('Clear completed history…',self.clear_history)
        samples=menu.addMenu('Diagnostics')
        samples.addAction('Add sample job',lambda:self.add_sample(False))
        samples.addAction('Add sample with a failure',lambda:self.add_sample(True))
        diagnostics.setMenu(menu)
        self.sample_button.hide()
        self.failure_button.hide()
        self.sample_button.setParent(history)
        self.failure_button.setParent(history)
        self.copy_button.setParent(history)
        self.copy_button.hide()
        for control in (self.undo_button, self.discard_analysis_button, self.archive_results_button):
            add.addWidget(control)
        add.addStretch()
        layout.addLayout(add)
        filters = FlowLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText('Search job name or tool')
        self.search.setAccessibleName('Search saved jobs')
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(340)
        self.search.setProperty('flowWidth',180)
        self.state_filter = QComboBox()
        for label, value in (('All statuses','all'), ('Waiting','queued'), ('Running','running'),
                             ('Completed','complete'), ('Needs attention','attention')):
            self.state_filter.addItem(label,value)
        self.state_filter.setAccessibleName('Filter job status')
        self.state_filter.setProperty('flowWidth',165)
        self.move_up_button = QPushButton('↑')
        self.move_down_button = QPushButton('↓')
        for control,name in ((self.move_up_button,'Move waiting entry up'),(self.move_down_button,'Move waiting entry down')):
            control.setFixedWidth(34)
            control.setAccessibleName(name)
            control.setToolTip(name+' in the saved order across all filters.')
        self.clear_history_button = QPushButton('Clear history')
        self.clear_history_button.setToolTip('Clear eligible completed entries across all filters. Files and standalone undo records are kept.')
        for control in (self.search,self.state_filter,self.move_up_button,self.move_down_button,self.clear_history_button,diagnostics):
            filters.addWidget(control)
        layout.addLayout(filters)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.jobs_model = Rows(("Job", "Status", "Finished / total", "Elapsed", "Created (UTC)"), self)
        self.jobs_table = table(self.jobs_model)
        self.jobs_table.setAccessibleName("Saved jobs")
        self.items_model = Rows(("File", "Status", "Attempts", "Details"), self)
        self.items_table = table(self.items_model)
        self.items_table.setAccessibleName("Job file results")
        self.items_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        splitter.addWidget(self.jobs_table)
        splitter.addWidget(self.items_table)
        self.child_model=Rows(('Step file','Status','Details'),self)
        self.child_table=table(self.child_model)
        self.child_table.setAccessibleName('Workflow step file results')
        self.child_table.horizontalHeader().setSectionResizeMode(2,QHeaderView.ResizeMode.Stretch)
        splitter.addWidget(self.child_table)
        self.jobs_splitter=splitter
        self.jobs_table.setMinimumHeight(64)
        self.items_table.setMinimumHeight(120)
        self.child_table.setMinimumHeight(64)
        self.child_table.hide()
        layout.addWidget(splitter, 1)
        self.detail = text("")
        self.detail.setToolTip("Select a file to see full source and output paths.")
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.detail.setMaximumHeight(100)
        layout.addWidget(self.detail)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.setAccessibleName('Overall job or workflow progress')
        layout.addWidget(self.progress)
        self.step_progress = QProgressBar()
        self.step_progress.setTextVisible(False)
        self.step_progress.setAccessibleName('Current file progress')
        self.step_progress.setFormat('Current file: %p%')
        self.step_progress.hide()
        layout.addWidget(self.step_progress)
        self.progress_detail = text('')
        layout.addWidget(self.progress_detail)
        self.status = text(f"Recovered {recovered} interrupted items. Retry checks saved outputs." if recovered else "")
        layout.addWidget(self.status)
        actions = FlowLayout()
        self.start_button = QPushButton("Start queue")
        self.start_button.setToolTip('Start all waiting entries in saved order, across all search/status filters.')
        self.start_selected_button=QPushButton('Start selected')
        self.pause_button = QPushButton("Pause queue")
        self.cancel_button = QPushButton("Cancel job")
        self.retry_button = QPushButton("Retry incomplete")
        self.export_button = QPushButton("Save report…")
        self.open_button = QPushButton("Open output folder")
        for control in (self.export_button,self.open_button):
            control.setParent(history)
            control.hide()
        for control in (self.start_button,self.start_selected_button, self.pause_button, self.cancel_button, self.retry_button,
                        ):
            actions.addWidget(control)
        layout.addLayout(actions)
        self.sample_button.clicked.connect(lambda: self.add_sample(False))
        self.failure_button.clicked.connect(lambda: self.add_sample(True))
        self.copy_button.clicked.connect(self.copy_current)
        self.start_button.clicked.connect(self.start_queue)
        self.start_selected_button.clicked.connect(self.start_selected)
        self.pause_button.clicked.connect(self.pause_queue)
        self.cancel_button.clicked.connect(self.cancel_job)
        self.retry_button.clicked.connect(self.retry_job)
        self.export_button.clicked.connect(self.export_report)
        self.open_button.clicked.connect(self.open_output)
        self.jobs_table.selectionModel().selectionChanged.connect(self.refresh_items)
        self.items_table.selectionModel().selectionChanged.connect(self.show_item)
        self.search.textChanged.connect(lambda:self.refresh())
        self.state_filter.currentIndexChanged.connect(lambda:self.refresh())
        self.move_up_button.clicked.connect(lambda:self.move_selected(-1))
        self.move_down_button.clicked.connect(lambda:self.move_selected(1))
        self.clear_history_button.clicked.connect(self.clear_history)
        from .help_ui import InfoButton
        filters.addWidget(InfoButton(lambda:'Review saved files, retry incomplete work or undo eligible renames/moves.'+
            ('\n\n'+self.detail.text() if self.detail.text() else '')+'\n\n'+self.clear_history_button.toolTip(),name='History information'))
        from .history_ui import HistoryLayout
        self.history_layout=HistoryLayout(self,history,layout)
        from .queue_views import QueueViews
        self.views=QueueViews(self,history,layout,actions)
        self.poll = QTimer(self)
        self.poll.setInterval(300)
        self.poll.timeout.connect(self.safe_refresh)
        self.poll.start()
        self.refresh()

    def show(self):
        if self.host.workspace_stack.indexOf(self)<0:self.host.workspace_stack.addWidget(self)
        self.host.workspace_stack.setCurrentWidget(self)
        for control in self.host.nav_group.buttons():control.setChecked(control.text()=='Queue')
        super().show()

    def hide(self):
        if self.host.workspace_stack.currentWidget() is self:
            self.host._tool_changed(self.host.tool)
        super().hide()

    def saved_jobs(self):
        return self.store.visible_jobs()

    def move_selected(self, direction):
        try:
            job = self.selected_job()
            if job:
                self.store.move_waiting(job, direction)
                self.refresh(job)
                self.status.setText('Waiting order saved. Start queue follows this order across all filters.')
        except Exception as error:
            self.message(error)

    def clear_history(self):
        try:
            if getattr(self.host,'archive_cleanup_worker',None) is not None:
                self.status.setText('Wait for source cleanup to finish before clearing history.')
                return
            protected_jobs=lambda:(self.worker.job_id,) if self.worker else ()
            count, protected = self.store.clear_completed(preview=True,protected_jobs=protected_jobs())
            if not count:
                self.status.setText(f'No eligible completed entries. {protected} kept for undo or recovery.')
                return
            answer = QMessageBox.question(self,'Clear completed history',
                f'Remove {count} completed queue entries and their step history across all filters?\n'
                'Files stay on disk. Records needed for undo or recovery are kept.',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:return
            if getattr(self.host,'archive_cleanup_worker',None) is not None:return
            count, protected = self.store.clear_completed(protected_jobs=protected_jobs())
            self.refresh()
            self.status.setText(f'Cleared {count} completed entries. {protected} kept for undo or recovery. Files kept.')
        except Exception as error:
            self.message(error)

    def start_selected(self):
        try:
            if self.selected_job():self.start_single(self.selected_job())
        except Exception as error:self.message(error)

    def showEvent(self, event):
        super().showEvent(event)
        self.safe_refresh(force=True)

    def selected_job(self):
        if hasattr(self,'views') and self.views.current():
            selected=self.views.selected()
            return selected['job_id'] if selected else None
        rows = self.jobs_table.selectionModel().selectedRows()
        return self.jobs_model.keys[rows[0].row()] if rows else None

    def selected_item(self):
        if hasattr(self,'views') and self.views.current():
            selected=self.views.selected()
            return selected['item_id'] if selected else None
        rows = self.items_table.selectionModel().selectedRows()
        return self.items_model.keys[rows[0].row()] if rows else None

    def message(self, error):
        self.status.setText(str(error))

    def safe_refresh(self, force=False):
        if self.closed or (not force and not self.isVisible()):
            return
        try:
            if not force:
                self.copy_button.setEnabled(not self.host.busy and self.plan_worker is None and not self.fatal)
                if not self.worker and not self.enabled:
                    return
                self.update_progress()
                self.update_live_elapsed()
                if self.store.db.execute("PRAGMA data_version").fetchone()[0] == self.data_version:
                    return
            self.refresh()
        except Exception as error:
            self.pause_queue()
            self.fatal = True
            self.message(f"History unavailable: {error}. Queue stopped.")
            self.poll.stop()

    def refresh(self, select=None):
        if not isinstance(select,str):select=None
        selected = select or self.selected_job()
        all_jobs = self.saved_jobs()
        query = self.search.text().strip().casefold()
        state = self.state_filter.currentData()
        from .queue_history import COMPLETE, duration
        def matches(job):
            status = job['state']
            return (not query or query in (job['title']+' '+job['adapter']).casefold()) and (
                state=='all' or status==state or state=='complete' and status in COMPLETE or
                state=='attention' and status in ('failed','interrupted','cancelled','blocked'))
        jobs = [job for job in all_jobs if matches(job)]
        elapsed = self.store.elapsed_times()
        # One grouped query avoids rereading every item's snapshot on every refresh.
        counts = {}
        for row in self.store.db.execute("SELECT job_id,state,count(*) AS n FROM items GROUP BY job_id,state"):
            counts.setdefault(row["job_id"], Counter())[row["state"]] = row["n"]
        rows = []
        for job in jobs:
            counter = counts.get(job["id"], Counter())
            finished = sum(counter[state] for state in ("succeeded", "failed", "skipped", "cancelled", "interrupted"))
            seconds=elapsed.get(job['id'],0)
            if self.worker and self.worker.job_id==job['id'] and self.worker.started_at:
                seconds=self.run_elapsed_base+time.monotonic()-self.worker.started_at
            rows.append((job["title"], job["state"], f"{finished} / {sum(counter.values())}",
                         duration(seconds) if job['id'] in elapsed or seconds else '—', job["created"][:19].replace("T", " ")))
        selection_blocker = QSignalBlocker(self.jobs_table.selectionModel())
        changed = self.jobs_model.replace([job["id"] for job in jobs], rows,
                                           [f"Job ID: {job['id']}" for job in jobs])
        if (changed or select is not None) and self.jobs_model.keys:
            key = selected if selected in self.jobs_model.keys else self.jobs_model.keys[-1]
            self.jobs_table.selectRow(self.jobs_model.keys.index(key))
            if select:self.jobs_table.scrollTo(self.jobs_table.currentIndex())
        selection_blocker.unblock()
        if hasattr(self,'views'):self.views.refresh(all_jobs,select)
        self.refresh_items()
        self.data_version = self.store.db.execute("PRAGMA data_version").fetchone()[0]
        self.start_button.setEnabled(not self.enabled and not self.fatal and any(job["state"] == "queued" for job in all_jobs))
        self.pause_button.setEnabled(self.enabled)
        self.copy_button.setEnabled(not self.host.busy and self.plan_worker is None and not self.fatal)
        self.copy_action.setEnabled(self.copy_button.isEnabled())
        self.start_selected_button.setEnabled(self.can_start_immediately and bool(self.selected_job()) and
            any(j['id']==self.selected_job() and j['state']=='queued' for j in all_jobs))
        self.update_progress()

    def update_live_elapsed(self):
        """Advance the active row without resetting tables or rereading history."""
        worker=self.worker
        if not worker or not worker.started_at or worker.job_id not in self.jobs_model.keys:
            return
        from .queue_history import duration
        row=self.jobs_model.keys.index(worker.job_id)
        values=list(self.jobs_model.rows[row])
        value=duration(self.run_elapsed_base+time.monotonic()-worker.started_at)
        if values[3]!=value:
            values[3]=value
            self.jobs_model.rows[row]=tuple(values)
            index=self.jobs_model.index(row,3)
            self.jobs_model.dataChanged.emit(index,index,[Qt.ItemDataRole.DisplayRole])

    def refresh_items(self, *args):
        job_id = self.selected_job()
        jobs = self.saved_jobs()
        selected = self.selected_item()
        items = self.store.items(job_id) if job_id else []
        rows, tips = [], []
        from .queue_history import duration
        elapsed=self.store.elapsed_times(live=True)
        for item in items:
            spec = json.loads(item["spec"])
            message=item['message']
            if 'request' in spec:
                child=json.loads(item['stage'] or '{}').get('child_job')
                if child in elapsed:message='Elapsed '+duration(elapsed[child])+' · '+message
            rows.append((spec["name"], item["state"], str(item["attempts"]), message))
            tips.append("\n".join(filter(None, (spec.get("source"), spec.get("output"), item["message"]))))
        changed = self.items_model.replace([item["id"] for item in items], rows, tips)
        if changed and selected in self.items_model.keys:
            self.items_table.selectRow(self.items_model.keys.index(selected))
        active = self.worker and self.worker.job_id == job_id
        self.start_selected_button.setEnabled(self.can_start_immediately and any(j['id']==job_id and j['state']=='queued' for j in jobs))
        self.retry_button.setEnabled(bool(job_id) and not active and not self.fatal and
                                     any(item["state"] in ("failed", "cancelled", "interrupted", "blocked") for item in items))
        retry_count=sum(i['state'] in ('failed','cancelled','interrupted','blocked') for i in items)
        self.retry_button.setToolTip(f'Retry {retry_count} incomplete '+('steps' if any('request' in json.loads(i['spec']) for i in items) else 'files')+'. Completed work is retained; required files are checked again.')
        waiting=[j['id'] for j in jobs if j['state']=='queued']
        position=waiting.index(job_id) if job_id in waiting else -1
        self.move_up_button.setEnabled(not self.fatal and position>0 and not active)
        self.move_down_button.setEnabled(not self.fatal and 0<=position<len(waiting)-1 and not active)
        self.clear_history_button.setEnabled(not self.fatal and getattr(self.host,'archive_cleanup_worker',None) is None)
        self.clear_history_action.setEnabled(self.clear_history_button.isEnabled())
        workflow=any(j['id']==job_id and j['adapter']=='workflow' for j in jobs)
        self.cancel_button.setEnabled(bool(job_id) and (bool(active) or any(item["state"] == "queued" or
            workflow and item['state'] in ('failed','interrupted','blocked') for item in items)))
        self.export_button.setEnabled(bool(job_id))
        self.export_action.setEnabled(self.export_button.isEnabled())
        self.support_action.setEnabled(self.export_button.isEnabled())
        self.open_button.setEnabled(any(json.loads(item['spec']).get('output') or
            ('request' in json.loads(item['spec']) and json.loads(item['stage'] or '{}').get('outputs')) for item in items))
        self.open_action.setEnabled(self.open_button.isEnabled())
        video = any(job["id"] == job_id and job["adapter"] == "video" for job in jobs)
        archives = any(job["id"] == job_id and job["adapter"] == "archives" for job in jobs)
        folders = any(job["id"] == job_id and job["adapter"] == "flatten" for job in jobs)
        audio = any(job["id"] == job_id and job["adapter"] == "audio" for job in jobs)
        documents = any(job["id"] == job_id and job["adapter"] == "documents" for job in jobs)
        self.archive_results_button.setText("Document results" if documents else "Audio results" if audio else "Folder results" if folders else "Archive results")
        self.archive_results_button.setVisible(archives or folders or audio or documents)
        self.archive_results_button.setEnabled((archives or folders or audio or documents) and self.can_start_immediately and not self.host.busy and bool(items))
        self.discard_analysis_button.setEnabled(video and not self.enabled and not self.worker and not self.fatal
                                               and any(item["stage"] for item in items))
        self.discard_analysis_button.setVisible(video)
        linked = self.store.undo_job(job_id) if job_id else None
        rename = any(job["id"] == job_id and job["adapter"] in ("rename", "flatten") for job in jobs)
        eligible = (rename and not self.store.original_job(job_id)
                    and any(item["state"] == "succeeded" for item in items)
                    and all(item["state"] in ("succeeded", "skipped") for item in items))
        self.undo_button.setText("View undo" if linked else "Preview undo")
        self.undo_button.setVisible(rename or bool(linked))
        self.undo_button.setEnabled(bool(linked) or (eligible and self.can_start_immediately and not self.host.busy))
        self.show_item()
        self.update_progress()

    def show_archive_results(self):
        job_id = self.selected_job()
        if not job_id or not self.can_start_immediately or self.host.busy:
            return
        if any(job["id"]==job_id and job["adapter"]=="documents" for job in self.saved_jobs()):
            self.host._tool_changed("Documents")
            self.host.show_rename_results(self.store.items(job_id),job_id,documents=True)
            self.host.status.setText("Saved document results.")
            self.hide()
            return
        if any(job["id"] == job_id and job["adapter"] == "audio" for job in self.saved_jobs()):
            self.host._tool_changed("Audio")
            self.host.show_rename_results(self.store.items(job_id), job_id, audio=True)
            self.host.status.setText("Saved audio results.")
            self.hide()
            return
        if any(job["id"] == job_id and job["adapter"] == "flatten" for job in self.saved_jobs()):
            self.host.show_folders().show_results(self.store, job_id)
            self.hide()
            return
        if not any(job["id"] == job_id and job["adapter"] == "archives" for job in self.saved_jobs()):
            return
        items = self.store.items(job_id)
        self.host._tool_changed("Archives")
        if items:
            self.host.format.setCurrentText(json.loads(items[0]["spec"]).get("archive_format", "zip").upper().replace("7Z", "7z"))
        self.host.show_rename_results(items, job_id, archives=True)
        self.host.status.setText("Saved archive results. Review source cleanup to move eligible originals to Recycle Bin / Trash.")
        self.hide()

    def preview_selected_undo(self):
        job_id = self.selected_job()
        if not job_id:
            return
        linked = self.store.undo_job(job_id)
        if linked:
            self.refresh(linked)
            self.status.setText("This is the saved undo. Retry incomplete items here if needed.")
        elif any(j["id"] == job_id and j["adapter"] == "flatten" for j in self.saved_jobs()):
            if self.host.show_folders().preview_undo(job_id):
                self.hide()
                self.host.raise_()
                self.host.activateWindow()
        elif self.host.preview_undo(job_id):
            self.hide()
            self.host.raise_()
            self.host.activateWindow()

    def show_item(self, *args):
        item_id = self.selected_item()
        if item_id:
            item = self.store.item(item_id)
            spec = json.loads(item["spec"])
            if 'request' in spec:
                from .workflow_ui import request_summary
                stage=json.loads(item['stage'] or '{}')
                current_outputs=[dict(s) for s in stage.get('outputs',[])]
                for later in self.store.items(item['job_id']):
                    if later['ordinal']<=item['ordinal'] or later['state']!='succeeded':continue
                    moves=json.loads(later['stage'] or '{}').get('renames',[])
                    for artifact in current_outputs:
                        for move in moves:
                            if artifact['source']==move['before'] and artifact['identity']==move['identity']:
                                artifact['source']=move['after']
                self.detail.setText('\n'.join((request_summary(spec['request']),item['message'],
                    *[s['source'] for s in current_outputs])))
                self.child_model.replace([],[])
                child=stage.get('child_job')
                if child:
                    rows=self.store.items(child)
                    self.child_model.replace([r['id'] for r in rows],[(json.loads(r['spec'])['name'],r['state'],r['message']) for r in rows],
                        ['\n'.join(filter(None,(json.loads(r['spec']).get('source'),json.loads(r['spec']).get('output'),r['message']))) for r in rows])
                self.history_layout.show_child(child,item_id,workflow=True)
            else:
                self.detail.setText("\n".join(filter(None, (spec.get("source", spec["name"]), spec.get("output"), item["message"]))))
                self.history_layout.show_child(None)
        else:
            self.detail.setText("")
            self.history_layout.show_child(None)

    def add_sample(self, fail=False):
        try:
            specs = [{"name": f"Sample item {index + 1}", "seconds": 0.8, "fail_once": fail and index == 1}
                     for index in range(6)]
            job_id = self.store.enqueue("Sample with a failure" if fail else "Sample job", "sample", specs)
            self.status.setText("Sample job queued. It does not change media files.")
            self.refresh(job_id)
            if self.enabled:
                self._next()
            return job_id
        except Exception as error:
            self.message(error)

    def copy_current(self):
        if self.host.busy or self.plan_worker:
            return
        try:
            self.host.apply_filters()
            selected = self.host.table.selectionModel().selectedRows()
            records = (tuple(self.host.model.items[self.host.proxy.mapToSource(index).row()] for index in selected)
                       if selected else tuple(record for record in self.host.model.items if self.host.proxy.spec.matches(record)))
            if not records:
                raise ValueError("Add real files to the workspace first. Uses highlighted files, or all visible files.")
            destination = QFileDialog.getExistingDirectory(self, "Choose a folder for verified copies")
            if not destination:
                return
            self.plan_worker = CopyPlanWorker(records, destination, self)
            self.plan_worker.planned.connect(self.review_copy)
            self.plan_worker.failed.connect(self.message)
            self.plan_worker.finished.connect(self.copy_plan_finished)
            self.copy_button.setEnabled(False)
            self.status.setText("Checking copy sources and output names…")
            self.plan_worker.start()
        except Exception as error:
            self.message(error)

    def copy_plan_finished(self):
        worker = self.plan_worker
        self.plan_worker = None
        worker.deleteLater()
        if not self.closed:
            self.copy_button.setEnabled(not self.host.busy)
            self.host.update_counts()

    def review_copy(self, specs):
        if self.host._closing or not self.isVisible():
            return
        try:
            preview = QDialog(self)
            preview.setWindowTitle("Review verified copy job")
            preview.resize(800, 490)
            layout = QVBoxLayout(preview)
            from .help_ui import InfoButton
            summary=QHBoxLayout()
            summary.addWidget(text(f"{len(specs)} files"),1)
            summary.addWidget(InfoButton('Originals are kept. Existing names get a numbered suffix; late collisions fail safely.'))
            layout.addLayout(summary)
            model = Rows(("Source", "Output"), preview)
            model.replace(list(range(len(specs))), [(spec["source"], spec["output"]) for spec in specs],
                          [spec["source"] + "\n" + spec["output"] for spec in specs])
            layout.addWidget(table(model))
            controls = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            controls.button(QDialogButtonBox.StandardButton.Ok).setText("Add to queue")
            controls.accepted.connect(preview.accept)
            controls.rejected.connect(preview.reject)
            layout.addWidget(controls)
            if preview.exec() == QDialog.DialogCode.Accepted and not self.host._closing:
                job_id = self.store.enqueue("Verified copies", "copy", specs)
                self.refresh(job_id)
                self.status.setText("Verified copy job queued.")
                if self.enabled:
                    self._next()
        except Exception as error:
            self.message(error)

    def reject(self):
        if self.plan_worker:
            self.plan_worker.requestInterruption()
        self.hide()

    def start_queue(self):
        if self.fatal:
            return
        self.enabled = True
        self.single_job = None
        self.status.setText("Queue running. One file at a time.")
        self._next()
        self.refresh()

    @property
    def can_start_immediately(self):
        return not self.closed and not self.fatal and not self.enabled and self.worker is None and self.plan_worker is None

    def start_single(self, job_id):
        if not self.can_start_immediately:
            raise ValueError("Another task is active. Wait for it, or queue this batch.")
        job = next(job for job in self.saved_jobs() if job["id"] == job_id)
        if job["state"] != "queued":
            raise ValueError("This job is not waiting to run.")
        self.single_job = job_id
        self.enabled = True
        self.status.setText("Running this undo. Other waiting jobs stay paused." if self.store.original_job(job_id) else
                            "Running this batch. Other waiting jobs stay paused.")
        self._next()
        self.refresh(job_id)

    def _next(self):
        if not self.enabled or self.worker or self.closed or self.fatal:
            return
        queued = next((job for job in self.saved_jobs() if job["state"] == "queued"
                       and (self.single_job is None or job["id"] == self.single_job)), None)
        if queued is None:
            self.enabled = False
            self.single_job = None
            self.status.setText("Queue finished. Review individual file results.")
            self.update_progress()
            self.host.update_controls()
            return
        waveform=getattr(self.host,'waveform',None)
        if waveform is not None and waveform.worker is not None:
            waveform.cancel()
            QTimer.singleShot(100,self._next)
            return
        if queued["adapter"] in ("rename", "images", "video", "audio", "documents", "archives", "flatten", "workflow") and self.host.busy:
            self.status.setText("Waiting for file intake or preview to finish…")
            QTimer.singleShot(100, self._next)
            return
        if queued["adapter"] == "archives":
            from .seven_archive import password_for, remember_password
            from PySide6.QtWidgets import QInputDialog, QLineEdit
            for item in self.store.items(queued["id"]):
                spec = json.loads(item["spec"])
                if item["state"] != "queued" or not spec.get("encrypted"):
                    continue
                try:
                    password_for(spec)
                except ValueError:
                    password, accepted = QInputDialog.getText(self, "Archive password",
                        "Enter the password for this saved archive batch:", QLineEdit.EchoMode.Password)
                    if not accepted:
                        self.enabled = False
                        self.single_job = None
                        self.status.setText("Archive is waiting for its password. Start Queue when ready.")
                        self.host.update_controls()
                        return
                    try:
                        remember_password(password, spec["password_token"])
                    except ValueError as error:
                        self.enabled = False
                        self.single_job = None
                        self.status.setText(str(error))
                        self.host.update_controls()
                        return
        if queued["adapter"]=="documents":
            from .private_values import resolve,remember
            from PySide6.QtWidgets import QInputDialog,QLineEdit
            tokens={}
            for item in self.store.items(queued["id"]):
                if item["state"]!="queued":continue
                spec=json.loads(item["spec"])
                for key,title in (("input_secret","Input PDF password"),("output_secret","Output PDF password")):
                    if spec.get(key):tokens[spec[key]]=title
            for token,title in tokens.items():
                try:
                    resolve(token)
                except ValueError:
                    password,accepted=QInputDialog.getText(self,title,"Re-enter the password for this saved job:",QLineEdit.EchoMode.Password)
                    if not accepted:
                        self.enabled=False
                        self.single_job=None
                        self.status.setText("Document job is waiting for its password.")
                        self.host.update_controls()
                        return
                    try:
                        remember(password,token)
                    except ValueError as error:
                        self.enabled=False
                        self.single_job=None
                        self.status.setText(str(error))
                        self.host.update_controls()
                        return
        if queued['adapter']=='workflow':
            from .workflow_ui import require_passwords
            try:
                ready=require_passwords(self,queued['id'])
            except Exception as error:
                self.status.setText(str(error))
                ready=False
            if not ready:
                self.enabled=False
                self.single_job=None
                self.status.setText('Workflow is waiting for its passwords. Start it again when ready.')
                self.host.update_controls()
                return
        self.worker = JobWorker(self.directory, queued["id"], self, adapter=queued["adapter"])
        self.live_progress = None
        self.run_baseline = sum(i['state'] in ('succeeded','skipped') for i in self.store.items(queued['id']))
        self.run_elapsed_base = self.store.elapsed_times().get(queued['id'],0)
        self.worker.progress.connect(self._progress)
        if hasattr(self.host,'visual'):
            self.worker.activity.connect(self.host.visual.activity_event)
            self.host.visual.job_started(queued)
        self.worker.renamed.connect(self.host.apply_rename_results)
        self.worker.failed.connect(self.worker_failed)
        self.worker.finished.connect(self.worker_finished)
        self.progress.setValue(0)
        self.host.update_controls()
        if self.host.folders_active:
            self.host.folders_page.status.setText(f"Queue is running: {queued['title']}.")
        self.worker.start()

    def _progress(self, item, done, total):
        self.live_progress = (item, done / max(total,1), bool(total))
        if hasattr(self.host,'visual'):
            self.host.visual.percent_event(item,done,total)
            return
        if self.worker is not None and self.worker.adapter in ("rename", "images", "video", "audio", "documents", "archives", "flatten", "workflow"):
            progress = self.host.folders_page.progress if self.host.folders_active else self.host.progress
            progress.setRange(0, 100 if total else 0)
            progress.setValue(done)

    def update_progress(self):
        """Equal item/step weighting; no invented duration for unlike workflow steps."""
        from .queue_history import duration
        worker=self.worker
        job_id=worker.job_id if worker else self.selected_job()
        if not job_id:
            self.progress.setRange(0,100)
            self.progress.setValue(0)
            self.step_progress.hide()
            self.progress_detail.setText('')
            return
        row=self.store.db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        job=dict(row) if row else None
        if not job:return
        items=self.store.items(job_id)
        workflow=job['adapter']=='workflow'
        done=sum(i['state'] in ('succeeded','skipped') for i in items)
        active=next((i for i in items if i['state'] in ('running','verifying','committing')),None)
        part=0
        detail=''
        known=False
        if active:
            live=self.live_progress if worker and self.live_progress and self.live_progress[0]==active['id'] else None
            part=live[1] if live else 0
            known=live[2] if live else False
            if workflow:
                child=json.loads(active['stage'] or '{}').get('child_job')
                files=self.store.items(child) if child else []
                complete=sum(i['state'] in ('succeeded','skipped') for i in files)
                # An earlier callback may belong to a child that has just finished.
                processing=any(i['state'] in ('running','verifying','committing') for i in files)
                part=(complete+(part if processing else 0))/len(files) if files else 0
                detail=f"Step {active['ordinal']+1}/{len(items)}: {json.loads(active['spec'])['name']} · {complete}/{len(files)} files" if child else 'Inspecting inputs for the next step…'
                if child:detail+=' · Step elapsed '+duration(self.store.elapsed_times(live=True).get(child,0))
            else:
                detail=f"{json.loads(active['spec'])['name']} · {active['state']}"
        recorded=self.store.elapsed_times()
        seconds=recorded.get(job_id,0)
        run_seconds=time.monotonic()-worker.started_at if worker and worker.started_at else 0
        if worker:seconds=self.run_elapsed_base+run_seconds
        estimate='ETA unavailable'
        completed_run=done-self.run_baseline
        if worker and not workflow and completed_run>=2 and len(items)>done and run_seconds>=1:
            estimate='Estimated remaining '+duration(run_seconds/completed_run*(len(items)-done))
        overall=round(100*(done+min(max(part,0),1))/max(len(items),1))
        self.progress.setRange(0,100)
        self.progress.setValue(overall)
        self.progress.setFormat(('Workflow steps' if workflow else 'Job files')+': %p%')
        self.step_progress.setVisible(bool(active))
        self.step_progress.setRange(0,100 if known else 0)
        if known:self.step_progress.setValue(round(100*self.live_progress[1]))
        if active:detail+=(' · Current file '+str(round(100*self.live_progress[1]))+'%' if known else ' · Working…')
        unit='steps' if workflow else 'files'
        elapsed=duration(seconds) if worker or job_id in recorded or job['state']=='queued' else 'unavailable'
        self.progress_detail.setText(f"{job['title']}: {done}/{len(items)} {unit} completed ({overall}%) · {job['state']} · Elapsed {elapsed} · {estimate}"+
                                    ('\n'+detail if detail else ''))

    def worker_failed(self, message):
        self.fatal = True
        self.enabled = False
        self.status.setText(message)

    def worker_finished(self):
        worker = self.worker
        direct = self.single_job == worker.job_id
        self.worker = None
        if hasattr(self.host,'visual'):self.host.visual.job_finished(worker)
        worker.deleteLater()
        self.live_progress = None
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        if direct:
            self.enabled = False
            self.single_job = None
        if worker.adapter=='workflow':
            self.status.setText('Workflow '+next(j['state'] for j in self.saved_jobs() if j['id']==worker.job_id)+'. Select a step to review its outputs and file results.')
        if worker.adapter == "flatten":
            try:
                items = self.store.items(worker.job_id)
                self.host.apply_folder_results(items)
                page=self.host.folders_page
                if page is not None:page.show_results(self.store, worker.job_id, focus=False)
                if direct:
                    self.status.setText(self.host.folders_page.status.text())
            except Exception as error:
                self.host.status.setText(f"Could not read folder results: {error}. Open Queue for details.")
        if worker.adapter in ("rename", "images", "video", "audio", "documents", "archives"):
            try:
                items = self.store.items(worker.job_id)
                if worker.adapter == "archives" and not any(item["state"] == "queued" for item in items):
                    from .seven_archive import forget_password
                    for item in items:
                        forget_password(json.loads(item["spec"]).get("password_token"))
                counts = Counter(item["state"] for item in items)
                is_undo = bool(self.store.original_job(worker.job_id))
                self.host.show_rename_results(items, worker.job_id, is_undo, images=worker.adapter == "images", video=worker.adapter == "video", archives=worker.adapter == "archives", audio=worker.adapter == "audio",documents=worker.adapter=="documents",focus=False)
                done = counts["succeeded"]
                incomplete = counts["failed"] + counts["cancelled"] + counts["interrupted"]
                remux = worker.adapter == "video" and bool(items) and all(
                    json.loads(item["spec"]).get("action") == "remux" for item in items if item["state"] != "skipped")
                audio_copy = worker.adapter == "audio" and bool(items) and all(
                    json.loads(item["spec"]).get("mode") == "copy" for item in items if item["state"] != "skipped")
                verb = "Copied" if audio_copy else "Created ZIPs for" if worker.adapter == "archives" else "Remuxed" if remux else "Converted" if worker.adapter in ("images", "video", "audio") else "Restored original names for" if is_undo else "Renamed"
                unit = "audio tracks" if worker.adapter == "audio" else "archives" if worker.adapter == "archives" else "files"
                if worker.adapter=="documents":
                    verb,unit="Processed","outputs"
                if worker.adapter == "archives":
                    verb = "Created"
                if incomplete:
                    message = f"{verb} {done} {unit}; {incomplete} incomplete. Open Queue for details or retry."
                else:
                    message = f"{verb} {done} {unit}. {counts['skipped']} skipped."
                if not hasattr(self.host,'visual'):
                    self.host.status.setText(message)
                if direct:
                    self.status.setText(message)
                page=self.host.folders_page
                if page is not None and getattr(page,'cancelled_job',None)==worker.job_id:
                    page.status.setText(f'{done} completed · {incomplete} incomplete')
                    page.cancelled_job=None
                if not hasattr(self.host,'visual'):
                    self.host.progress.setRange(0, 100)
                    self.host.progress.setValue(100 if not incomplete else 0)
            except Exception as error:
                self.host.status.setText(f"Could not read operation results: {error}. Open Queue for details.")
        self.host.update_controls()
        if not self.closed:
            self.safe_refresh(force=True)
            if not direct:
                QTimer.singleShot(0, self._next)

    def pause_queue(self):
        self.enabled = False
        if self.worker:
            self.worker.paused.set()
        self.status.setText("Pause requested. The rename batch finishes or restores its original names first."
                            if self.worker and self.worker.adapter == "rename" else
                            "Queue paused. The current file finishes; remaining files wait.")
        self.host.update_controls()

    def cancel_job(self):
        job_id = self.selected_job()
        if not job_id:
            return
        try:
            if self.worker and self.worker.job_id == job_id:
                self.worker.cancelled.set()
                self.status.setText("Cancel requested. A rename batch already applying final names finishes safely."
                                    if self.worker.adapter == "rename" else "Cancelling this job…")
            else:
                self.store.cancel_queued(job_id)
                self.refresh()
        except Exception as error:
            self.message(error)

    def retry_job(self):
        job_id = self.selected_job()
        if not job_id or (self.worker and self.worker.job_id == job_id):
            return
        try:
            count = self.store.retry(job_id)
            self.status.setText(f"{count} incomplete items queued. Successful and skipped items are kept.")
            self.refresh(job_id)
            if self.enabled:
                self._next()
        except Exception as error:
            self.message(error)

    def discard_analysis(self):
        job_id = self.selected_job()
        if not job_id or self.enabled or self.worker or self.fatal:
            return
        if not any(job["id"] == job_id and job["adapter"] == "video" for job in self.saved_jobs()):
            return
        try:
            from .video_stats import cleanup_scope
            count = 0
            for item in self.store.items(job_id):
                if item["stage"]:
                    cleanup_scope(self.store, item["id"])
                    self.store.set_item(item["id"], item["state"], "Saved analysis discarded; Retry will restart both passes.")
                    count += 1
            self.status.setText(f"Discarded analysis for {count} files. Sources and outputs kept.")
            self.refresh(job_id)
        except Exception as error:
            self.message(error)

    def export_report(self):
        job_id = self.selected_job()
        if not job_id:
            return
        filename, _ = QFileDialog.getSaveFileName(self, "Save job report", "media-tool-job.json", "JSON (*.json)")
        if filename:
            try:
                job = next(job for job in self.saved_jobs() if job["id"] == job_id)
                report={"job": job, "items": self.store.items(job_id), "events": self.store.events(job_id),
                        'elapsed_seconds':self.store.elapsed_times().get(job_id)}
                if job['adapter']=='workflow':
                    report['steps']=[]
                    for item in report['items']:
                        child=json.loads(item['stage'] or '{}').get('child_job')
                        if child:report['steps'].append(dict(step=item['id'],job=next(j for j in self.store.jobs() if j['id']==child),
                            items=self.store.items(child),events=self.store.events(child),elapsed_seconds=self.store.elapsed_times().get(child)))
                Path(filename).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                self.status.setText("Job report saved.")
            except Exception as error:
                self.message(error)

    def open_output(self):
        from .output_paths import job_output_paths,open_output_path
        paths=job_output_paths(self.store,self.selected_job(),self.selected_item())
        path=next((p for p in paths if Path(p).exists()),paths[0] if paths else None)
        return open_output_path(path,self.status.setText,folder=True)

    def export_support_report(self):
        job_id=self.selected_job()
        if not job_id:return
        filename,_=QFileDialog.getSaveFileName(self,'Save support report','media-tool-support.json','JSON (*.json)')
        if filename:
            try:
                from .support_report import make_support_report
                report=make_support_report(self.store,job_id)
                Path(filename).write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
                self.status.setText('Support report saved.')
            except Exception as error:self.message(error)

    def shutdown(self):
        """Called repeatedly by the main close event; never destroy a running QThread."""
        self.enabled = False
        self.poll.stop()
        if self.plan_worker:
            self.plan_worker.requestInterruption()
        if self.worker:
            self.worker.cancelled.set()
            self.worker.paused.set()
        if self.worker or self.plan_worker:
            return False
        if not self.closed:
            self.closed = True
            self.store.close()
            self.owner.close()
        return True


# Retained for source integrations/tests; this is an embedded QWidget page.
QueueDialog=QueuePage
