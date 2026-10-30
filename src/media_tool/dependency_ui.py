"""On-demand dependency checks and user-selected Windows package installations."""
import os
import re

from PySide6.QtCore import QObject,QProcess,QTimer,Signal,Qt,QUrl,QEvent
from PySide6.QtGui import QDesktopServices,QTextCursor
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,
    QCheckBox,QFrame,QScrollArea,QProgressBar,QPlainTextEdit)

from .dependencies import TOOLS,BY_KEY,PROBES,install_arguments
from .flow_layout import FlowLayout
from .help_ui import InfoButton
from .services import ToolProbe


def short_version(message):
    first=message.splitlines()[0]
    match=re.search(r'(?:7-Zip|ffmpeg version|ffprobe version|tesseract|LibreOffice)\s+([0-9v][^\s:]*)',first,re.I)
    value=match[1] if match else first.split(': ',1)[-1]
    return value if len(value)<=48 else value[:47]+'…'


class PackageInstaller(QObject):
    output=Signal(str)
    finished=Signal(str,bool,str)

    def __init__(self,parent=None):
        super().__init__(parent)
        self.process=QProcess(self)
        self.process.readyReadStandardOutput.connect(self.read)
        self.process.readyReadStandardError.connect(self.read)
        self.process.finished.connect(self.complete)
        self.process.errorOccurred.connect(self.error)
        self.key=None
        self.tail=''

    @property
    def active(self):return self.key is not None

    def start(self,key,program,prefix=()):
        if self.active:return False
        arguments=install_arguments(key)  # Reject unknown packages before launch.
        self.key=key
        self.tail=''
        self.process.setProgram(program)
        self.process.setArguments([*prefix,*arguments])
        self.process.start()
        self.process.closeWriteChannel()
        return True

    def read(self):
        data=bytes(self.process.readAllStandardOutput())+bytes(self.process.readAllStandardError())
        text=data.decode('utf-8',errors='replace')
        text=re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]','',text).replace('\r','\n')
        if text:
            self.tail=(self.tail+text)[-32768:]
            self.output.emit(text[-32768:])

    def complete(self,code,status):
        self.read()
        if self.key is None:return
        key,self.key=self.key,None
        ok=code==0 and status==QProcess.ExitStatus.NormalExit
        message='Installer finished.' if ok else f'Installer exited with code {code} (0x{code & 0xffffffff:08X}).'
        self.finished.emit(key,ok,message)

    def error(self,error):
        if error==QProcess.ProcessError.FailedToStart and self.key is not None:
            key,self.key=self.key,None
            self.finished.emit(key,False,'Could not start Windows Package Manager: '+self.process.errorString())


class DependencyPage(QWidget):
    def __init__(self,host):
        super().__init__(host)
        self.host=host
        self.setObjectName('workspace')
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground,True)
        self.previous=None
        self.results={}
        self.states={tool.key:'Checking' for tool in TOOLS}
        self.checking=False
        self.batch=False
        self.closing=False
        self.pending=[]
        self.batch_results=[]
        self.after_install=None
        self.winget=None
        self.probe=ToolProbe(self)
        self.probe.checked.connect(self.checked)
        self.next_check=QTimer(self)
        self.next_check.setSingleShot(True)
        self.next_check.timeout.connect(self.check_next)
        self.activation=QTimer(self)
        self.activation.setSingleShot(True)
        self.activation.timeout.connect(self.check)
        host.installEventFilter(self)
        self.installer=PackageInstaller(self)
        self.installer.output.connect(self.append_log)
        self.installer.finished.connect(self.installed)
        layout=QVBoxLayout(self)
        layout.setContentsMargins(18,16,18,14)
        layout.setSpacing(12)
        heading=QHBoxLayout()
        title=QLabel('Dependencies')
        title.setObjectName('heading')
        heading.addWidget(title,1)
        heading.addWidget(InfoButton('Checks run only when this page is opened, refreshed, or an installation finishes. Download opens the project’s download page. Select missing tools and accept their package/source agreements to install through Windows Package Manager. Windows may request administrator approval. Existing working tools are not upgraded. These external programs install outside Media Tool; the app’s built-in image/PDF libraries are already supplied.'))
        layout.addLayout(heading)
        toolbar=FlowLayout()
        self.back=QPushButton('Back')
        self.back.clicked.connect(self.go_back)
        self.refresh=QPushButton('Check again')
        self.refresh.clicked.connect(lambda:self.check())
        self.select_missing=QPushButton('Select missing')
        self.select_missing.clicked.connect(self.select_all_missing)
        for button in (self.back,self.refresh,self.select_missing):toolbar.addWidget(button)
        layout.addLayout(toolbar)
        scroll=QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body=QWidget()
        column=QVBoxLayout(body)
        column.setContentsMargins(0,0,4,0)
        column.setSpacing(10)
        self.rows={}
        for tool in TOOLS:
            card=QFrame()
            card.setObjectName('panel')
            card_layout=QVBoxLayout(card)
            card_layout.setContentsMargins(12,10,12,10)
            row=QHBoxLayout()
            selected=QCheckBox(tool.title)
            selected.setAccessibleName('Select '+tool.title+' for installation')
            selected.toggled.connect(self.controls)
            row.addWidget(selected,1)
            state=QLabel('Checking')
            state.setObjectName('badge')
            row.addWidget(state)
            download=QPushButton('Download')
            download.clicked.connect(lambda checked=False,key=tool.key:self.download(key))
            row.addWidget(download)
            row.addWidget(InfoButton(lambda key=tool.key:self.information(key),name=tool.title+' details'))
            card_layout.addLayout(row)
            description=QLabel(tool.purpose)
            description.setObjectName('muted')
            description.setWordWrap(True)
            description.setTextFormat(Qt.TextFormat.PlainText)
            card_layout.addWidget(description)
            column.addWidget(card)
            self.rows[tool.key]=dict(selected=selected,state=state,description=description,download=download)
        column.addStretch()
        scroll.setWidget(body)
        layout.addWidget(scroll,1)
        self.service=QLabel()
        self.service.setObjectName('warning')
        self.service.setWordWrap(True)
        layout.addWidget(self.service)
        agreements=QHBoxLayout()
        self.agreements=QCheckBox('Accept package/source agreements')
        self.agreements.toggled.connect(self.controls)
        agreements.addWidget(self.agreements)
        agreements.addWidget(InfoButton('Install uses the fixed package IDs from Microsoft’s winget community source and accepts the selected packages’ and source’s agreements. Open each Download page to review the publisher and licensing. Nothing installs until you choose Install selected.'))
        agreements.addStretch()
        layout.addLayout(agreements)
        actions=FlowLayout()
        self.install=QPushButton('Install selected')
        self.install.setObjectName('primary')
        self.install.clicked.connect(self.install_selected)
        self.stop=QPushButton('Stop after current')
        self.stop.clicked.connect(self.stop_after_current)
        self.details=QPushButton('Details')
        self.details.setCheckable(True)
        self.details.toggled.connect(lambda visible:self.log.setVisible(visible))
        for button in (self.install,self.stop,self.details):actions.addWidget(button)
        layout.addLayout(actions)
        self.progress=QProgressBar()
        self.progress.setRange(0,0)
        self.progress.setTextVisible(False)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.status=QLabel('')
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.log=QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(200)
        self.log.setMaximumHeight(140)
        self.log.hide()
        layout.addWidget(self.log)
        self.controls()

    def eventFilter(self,watched,event):
        if event.type()==QEvent.Type.WindowActivate and self.isVisible() and not self.busy and not self.closing:
            self.activation.start(250)
        return False

    @property
    def busy(self):return self.batch or self.checking or self.installer.active

    def go_back(self):
        if self.previous is not None:self.host.workspace_stack.setCurrentWidget(self.previous)
        else:self.host._tool_changed(self.host.tool)

    def download(self,key):
        if QDesktopServices.openUrl(QUrl(BY_KEY[key].download)):
            self.status.setText(BY_KEY[key].title+' download page opened. Check again after installation.')
        else:self.status.setText('Could not open the download page. '+BY_KEY[key].download)

    def information(self,key):
        tool=BY_KEY[key]
        messages=[self.results[name]['message'] for name in tool.programs if name in self.results]
        return '\n\n'.join([tool.help,'Package: '+tool.package,'Download: '+tool.download,*messages])

    def check(self,after_install=False):
        if self.closing or self.checking or self.installer.active or self.batch and not after_install:return
        self.activation.stop()
        self.results={}
        self.winget=None
        self.checking=True
        self.check_queue=list(PROBES)
        self.status.setText('Checking dependencies…')
        self.update_rows()
        self.controls()
        self.next_check.start(0)

    def check_next(self):
        if self.closing:return
        if self.probe.process.state()!=QProcess.ProcessState.NotRunning:
            self.next_check.start(10)
            return
        if self.check_queue:
            name=self.check_queue.pop(0)
            self.probe.start(name,PROBES[name])
            return
        self.checking=False
        manager=self.results.get('winget',{})
        self.winget=manager.get('path') if manager.get('success') and os.name=='nt' else None
        self.service.setText('' if self.winget else 'Automatic installation is unavailable. Use Download, then Check again.')
        self.service.setVisible(not bool(self.winget))
        self.update_rows()
        if self.batch:
            if self.after_install:
                key,success,message=self.after_install
                ready=self.states[key]=='Installed'
                self.batch_results.append((key,ready))
                if not ready:self.append_log(BY_KEY[key].title+': '+message+' Tool is not ready; inspect Details or use Download.\n')
                self.after_install=None
            QTimer.singleShot(0,self.start_next)
        else:
            count=sum(state=='Installed' for state in self.states.values())
            self.status.setText(f'{count} of {len(TOOLS)} dependencies ready.')
        self.controls()

    def checked(self,result):
        if self.closing:return
        self.results[result['name']]=result
        self.update_rows()
        self.next_check.start(0)

    def update_rows(self):
        for tool in TOOLS:
            values=[self.results.get(name) for name in tool.programs]
            if any(value is None for value in values):state='Checking'
            elif all(value['success'] for value in values):state='Installed'
            elif any(value['success'] for value in values):state='Incomplete'
            elif any(value['path'] for value in values):state='Check failed'
            else:state='Missing'
            self.states[tool.key]=state
            row=self.rows[tool.key]
            row['state'].setText('Installing…' if self.installer.key==tool.key else state)
            versions=[short_version(value['message']) for value in values if value and value['success']]
            row['description'].setText(tool.purpose+(' · '+versions[0] if versions else ''))
            if state=='Installed':row['selected'].setChecked(False)

    def controls(self,*args):
        if not hasattr(self,'install'):return
        idle=not self.busy and not self.closing
        self.refresh.setEnabled(idle)
        self.select_missing.setEnabled(idle)
        self.agreements.setEnabled(idle)
        for key,row in self.rows.items():row['selected'].setEnabled(idle and self.states[key] in ('Missing','Incomplete','Check failed'))
        selected=any(row['selected'].isChecked() and self.states[key]!='Installed' for key,row in self.rows.items())
        self.install.setEnabled(idle and bool(self.winget) and self.agreements.isChecked() and selected)
        self.stop.setVisible(self.batch)
        self.stop.setEnabled(self.batch and bool(self.pending))

    def select_all_missing(self):
        if self.busy:return
        for key,row in self.rows.items():row['selected'].setChecked(self.states[key] in ('Missing','Incomplete','Check failed'))

    def install_selected(self):
        if self.busy or self.closing or not self.winget or not self.agreements.isChecked():return
        self.pending=[key for key,row in self.rows.items() if row['selected'].isChecked() and self.states[key] in ('Missing','Incomplete','Check failed')]
        if not self.pending:return
        self.batch=True
        self.batch_results=[]
        self.progress.show()
        self.start_next()

    def start_next(self):
        if self.closing or not self.pending or not self.winget:
            self.pending=[]
            self.batch=False
            self.progress.hide()
            ready=sum(ok for _,ok in self.batch_results)
            failed=sum(not ok for _,ok in self.batch_results)
            self.status.setText(f'Installation checks finished: {ready} ready, {failed} not ready.')
            self.controls()
            return
        key=self.pending.pop(0)
        if self.states[key]=='Installed':
            self.batch_results.append((key,True))
            QTimer.singleShot(0,self.start_next)
            return
        self.status.setText('Installing '+BY_KEY[key].title+'…')
        self.append_log('\n'+BY_KEY[key].title+' · '+BY_KEY[key].package+'\n')
        self.installer.start(key,self.winget)
        self.update_rows()
        self.controls()

    def installed(self,key,success,message):
        self.append_log(message+'\n')
        if self.closing:
            self.batch=False
            return
        self.after_install=(key,success,message)
        self.check(after_install=True)

    def append_log(self,text):
        self.log.setPlainText((self.log.toPlainText()+text)[-32768:])
        self.log.moveCursor(QTextCursor.MoveOperation.End)
        lines=[line.strip() for line in text.splitlines() if line.strip()]
        if self.installer.active and lines:self.status.setText(BY_KEY[self.installer.key].title+': '+lines[-1][-250:])

    def stop_after_current(self):
        self.pending=[]
        self.status.setText('The current installation will finish. Remaining installations were removed.')
        self.controls()

    def shutdown(self):
        self.closing=True
        self.pending=[]
        self.activation.stop()
        self.next_check.stop()
        self.probe.cancel()
        if self.installer.active:
            self.status.setText('Finishing '+BY_KEY[self.installer.key].title+' installation before closing…')
        return not self.installer.active and self.probe.process.state()==QProcess.ProcessState.NotRunning
