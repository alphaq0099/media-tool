import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject,QProcess,QTimer,Signal
from PySide6.QtTest import QTest

from media_tool import dependency_ui,tool_paths
from media_tool.dependencies import TOOLS,BY_KEY,install_arguments
from media_tool.dependency_ui import PackageInstaller
from test_foundation import app,window,wait_until


@pytest.fixture
def setup_manager(window,monkeypatch):
    installed={'winget','7z','ffmpeg','ffprobe'}
    started=[]
    behavior=dict(hold=False,fail=set(),no_effect=set())

    class Probe(QObject):
        checked=Signal(object)
        def __init__(self,parent):
            super().__init__(parent)
            self.process=SimpleNamespace(state=lambda:QProcess.ProcessState.NotRunning)
        def start(self,name,args):
            present=name in installed
            value=dict(name=name,path='C:/fixture-tools/'+name+'.exe' if present else None,success=present,
                message=name+(': fixture version 1\nLocation: C:/fixture-tools/'+name+'.exe' if present else ': not found.'))
            QTimer.singleShot(0,lambda:self.checked.emit(value))
        def cancel(self):pass

    class Installer(QObject):
        output=Signal(str)
        finished=Signal(str,bool,str)
        def __init__(self,parent):
            super().__init__(parent)
            self.key=None
        @property
        def active(self):return self.key is not None
        def start(self,key,program):
            assert self.key is None,'An installer was started concurrently.'
            self.key=key
            started.append(key)
            if not behavior['hold']:QTimer.singleShot(15,self.finish)
        def finish(self):
            key,self.key=self.key,None
            ok=key not in behavior['fail']
            if ok and key not in behavior['no_effect']:installed.update(BY_KEY[key].programs)
            self.finished.emit(key,ok,'Fixture installed.' if ok else 'Fixture download failed.')

    monkeypatch.setattr(dependency_ui,'ToolProbe',Probe)
    monkeypatch.setattr(dependency_ui,'PackageInstaller',Installer)
    window.show_dependencies()
    page=window.dependencies_page
    wait_until(lambda:not page.busy)
    return SimpleNamespace(page=page,installed=installed,started=started,behavior=behavior)


def test_opening_manager_only_checks_and_preserves_workspace(window,setup_manager):
    env=setup_manager
    page=env.page
    assert window.workspace_stack.currentWidget() is page
    assert not page.isWindow()
    assert page.states=={'7zip':'Installed','ffmpeg':'Installed','tesseract':'Missing','libreoffice':'Missing'}
    assert not page.install.isEnabled()
    assert env.started==[]
    page.select_all_missing()
    assert [key for key,row in page.rows.items() if row['selected'].isChecked()]==['tesseract','libreoffice']
    assert not page.rows['7zip']['selected'].isEnabled()
    page.go_back()
    assert window.workspace_stack.currentWidget() is window.main_workspace


@pytest.mark.skipif(os.name!='nt',reason='Windows package installation controls')
def test_only_selected_missing_tools_install_sequentially_and_are_rechecked(setup_manager):
    env=setup_manager
    page=env.page
    page.select_all_missing()
    page.install_selected()
    assert env.started==[]
    page.agreements.setChecked(True)
    page.install_selected()
    wait_until(lambda:not page.busy)
    assert env.started==['tesseract','libreoffice']
    assert set(page.states.values())=={'Installed'}
    assert not any(row['selected'].isChecked() for row in page.rows.values())
    assert '2 ready, 0 not ready' in page.status.text()


@pytest.mark.skipif(os.name!='nt',reason='Windows package installation controls')
def test_zero_exit_without_usable_tool_is_not_reported_as_installed(setup_manager):
    env=setup_manager
    env.behavior['no_effect'].add('tesseract')
    page=env.page
    page.rows['tesseract']['selected'].setChecked(True)
    page.agreements.setChecked(True)
    page.install_selected()
    wait_until(lambda:not page.busy)
    assert page.states['tesseract']=='Missing'
    assert '0 ready, 1 not ready' in page.status.text()
    assert 'Tool is not ready' in page.log.toPlainText()


@pytest.mark.skipif(os.name!='nt',reason='Windows package installation controls')
def test_failed_install_does_not_prevent_other_selected_tool(setup_manager):
    env=setup_manager
    env.behavior['fail'].add('tesseract')
    page=env.page
    page.select_all_missing()
    page.agreements.setChecked(True)
    page.install_selected()
    wait_until(lambda:not page.busy)
    assert page.states['tesseract']=='Missing'
    assert page.states['libreoffice']=='Installed'
    assert '1 ready, 1 not ready' in page.status.text()
    assert 'download failed' in page.log.toPlainText()


@pytest.mark.skipif(os.name!='nt',reason='Windows package installation controls')
def test_stop_after_current_does_not_kill_current_installer(setup_manager):
    env=setup_manager
    env.behavior['hold']=True
    page=env.page
    page.select_all_missing()
    page.agreements.setChecked(True)
    page.install_selected()
    assert page.installer.active
    page.stop_after_current()
    assert page.installer.active
    page.installer.finish()
    wait_until(lambda:not page.busy)
    assert env.started==['tesseract']
    assert page.states['libreoffice']=='Missing'


@pytest.mark.skipif(os.name!='nt',reason='Windows package installation controls')
def test_close_waits_for_active_installer_and_discards_remaining_queue(window,setup_manager):
    env=setup_manager
    env.behavior['hold']=True
    page=env.page
    page.select_all_missing()
    page.agreements.setChecked(True)
    page.install_selected()
    window.close()
    assert window.isVisible()
    assert page.installer.active
    assert page.pending==[]
    page.installer.finish()
    wait_until(lambda:not window.isVisible())
    assert env.started==['tesseract']


def test_partial_ffmpeg_and_download_fallback(setup_manager,monkeypatch):
    env=setup_manager
    env.installed.remove('ffprobe')
    env.installed.remove('winget')
    page=env.page
    page.check()
    wait_until(lambda:not page.busy)
    assert page.states['ffmpeg']=='Incomplete'
    assert page.service.isVisible()
    page.select_all_missing()
    page.agreements.setChecked(True)
    assert not page.install.isEnabled()
    opened=[]
    monkeypatch.setattr(dependency_ui.QDesktopServices,'openUrl',lambda url:opened.append(url.toString()) or True)
    page.rows['ffmpeg']['download'].click()
    assert opened==['https://www.gyan.dev/ffmpeg/builds/']
    assert 'page opened' in page.status.text()


def test_return_to_queue_keeps_tab_and_logs_are_bounded(window,setup_manager):
    page=setup_manager.page
    page.go_back()
    window.open_jobs()
    queue=window.jobs_dialog
    queue.tabs.setCurrentWidget(queue.views.history)
    window.show_dependencies()
    wait_until(lambda:not page.busy)
    page.append_log('x'*200000)
    assert len(page.log.toPlainText())<=32768
    page.go_back()
    assert window.workspace_stack.currentWidget() is queue
    assert queue.tabs.currentWidget() is queue.views.history


def test_install_command_is_fixed_exact_and_does_not_force_upgrades():
    for tool in TOOLS:
        arguments=install_arguments(tool.key)
        assert arguments[:4]==['install','--id',tool.package,'--exact']
        assert arguments[arguments.index('--source')+1]=='winget'
        assert '--no-upgrade' in arguments
        assert '--ignore-security-hash' not in arguments and '--force' not in arguments and '--allow-reboot' not in arguments
    with pytest.raises(KeyError):install_arguments('arbitrary-user-package')


@pytest.mark.parametrize('exit_code',[0,7])
def test_installer_process_arguments_output_and_failure_are_reported(app,tmp_path,exit_code):
    helper=tmp_path/'installer fixture.py'
    arguments=tmp_path/'arguments.json'
    helper.write_text('import json,sys\nfrom pathlib import Path\n'+
        f'Path({str(arguments)!r}).write_text(json.dumps(sys.argv[1:]))\n'+
        f'print("x"*100000)\nraise SystemExit({exit_code})\n')
    installer=PackageInstaller()
    outcomes=[]
    installer.finished.connect(lambda *values:outcomes.append(values))
    assert installer.start('7zip',sys.executable,[str(helper)])
    assert not installer.start('ffmpeg',sys.executable,[str(helper)])
    wait_until(lambda:not installer.active)
    assert len(outcomes)==1
    assert outcomes[0][0:2]==('7zip',exit_code==0)
    assert json.loads(arguments.read_text())==install_arguments('7zip')
    assert len(installer.tail)<=32768
    assert installer.process.state()==QProcess.ProcessState.NotRunning


def test_installer_failed_to_start_recovers_controls(app,tmp_path):
    installer=PackageInstaller()
    outcomes=[]
    installer.finished.connect(lambda *values:outcomes.append(values))
    installer.start('7zip',str(tmp_path/'missing winget.exe'))
    wait_until(lambda:not installer.active)
    assert outcomes[0][0:2]==('7zip',False)
    assert 'Could not start' in outcomes[0][2]


@pytest.mark.skipif(os.name != 'nt', reason='Windows registered PATH discovery')
@pytest.mark.parametrize('name',['7z','ffmpeg','ffprobe','tesseract','soffice'])
def test_newly_installed_tools_are_found_without_changing_process_path(monkeypatch,tmp_path,name):
    for variable in ('ProgramW6432','ProgramFiles','ProgramFiles(x86)','LOCALAPPDATA'):monkeypatch.delenv(variable,raising=False)
    monkeypatch.setattr(tool_paths.shutil,'which',lambda name:None)
    monkeypatch.setattr(tool_paths,'_seven_zip_registry',lambda:())
    monkeypatch.setattr(tool_paths,'_document_folders',lambda name:())
    monkeypatch.setattr(tool_paths,'_windows_path_folders',lambda:[tmp_path])
    path=tmp_path/(name+('.exe' if os.name=='nt' else ''))
    path.write_bytes(b'Lookup fixture only, never executed')
    old_path=os.environ.get('PATH')
    assert tool_paths.resolve_tool(name)==str(path)
    assert os.environ.get('PATH')==old_path


def test_document_workers_use_the_same_tool_discovery(monkeypatch,tmp_path):
    from media_tool import document_external
    monkeypatch.setattr(tool_paths,'resolve_tool',lambda name:str(tmp_path/(name+'.exe')))
    assert document_external.available_tools()=={name:str(tmp_path/(name+'.exe')) for name in ('tesseract','soffice')}


@pytest.mark.skipif(os.name!='nt',reason='Windows registered PATH and WinGet links')
def test_registered_path_entries_and_portable_links_are_read_without_mutation(monkeypatch,tmp_path):
    local=tmp_path/'Local AppData'
    binary=local/'New tools'
    binary.mkdir(parents=True)
    (binary/'tesseract.exe').write_bytes(b'Never executed')
    links=local/'Microsoft'/'WinGet'/'Links'
    links.mkdir(parents=True)
    (links/'ffprobe.exe').write_bytes(b'Never executed')
    monkeypatch.setenv('LOCALAPPDATA',str(local))
    for name in ('ProgramW6432','ProgramFiles','ProgramFiles(x86)'):monkeypatch.delenv(name,raising=False)
    class Key:
        def __init__(self,hive):self.hive=hive
        def __enter__(self):return self
        def __exit__(self,*args):pass
    registry=SimpleNamespace(HKEY_LOCAL_MACHINE=1,HKEY_CURRENT_USER=2,
        OpenKey=lambda hive,key:Key(hive),
        QueryValueEx=lambda key,name:('relative;;"%LOCALAPPDATA%/New tools";' if key.hive==2 else '',2))
    monkeypatch.setitem(sys.modules,'winreg',registry)
    monkeypatch.setattr(tool_paths.shutil,'which',lambda name:None)
    old_path=os.environ.get('PATH')
    assert binary in tool_paths._windows_path_folders()
    assert tool_paths.resolve_tool('tesseract')==str(binary/'tesseract.exe')
    assert tool_paths.resolve_tool('ffprobe')==str(links/'ffprobe.exe')
    assert os.environ.get('PATH')==old_path


def test_structured_probe_result_keeps_original_identity_during_reentry(app):
    from media_tool.services import ToolProbe
    probe=ToolProbe()
    values=[]
    reentered=[]
    def next_tool(message):
        if not reentered:
            reentered.append(True)
            probe.start('deliberately-missing-reentry-tool')
    probe.result.connect(next_tool)
    probe.checked.connect(values.append)
    probe.start(sys.executable,['--version'])
    wait_until(lambda:len(values)==2)
    assert values[0]['name']=='deliberately-missing-reentry-tool' and not values[0]['success']
    assert values[1]['name']==sys.executable and values[1]['success']
    assert values[1]['path'] is not None
