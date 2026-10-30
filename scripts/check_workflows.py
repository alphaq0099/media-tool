"""Native embedded Queue/workflow trial with disposable media/settings/history."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from PySide6.QtCore import QSettings,Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication
from media_tool import __version__
from media_tool.domain import FileRecord
from media_tool.tool_paths import resolve_tool
from media_tool.window import Workspace


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--report',type=Path,required=True)
    report=parser.parse_args().report.resolve()
    report.parent.mkdir(parents=True,exist_ok=True)
    app=QApplication([])
    app.setStyle('Fusion')
    app.setFont(QFont('Segoe UI',10))
    result=dict(version=__version__,passed=False,qt_platform=app.platformName(),screenshots={},layouts=[])
    def wait(predicate):
        limit=time.monotonic()+60
        while not predicate():
            if time.monotonic()>limit:raise TimeoutError('Workflow UI trial timed out')
            app.processEvents()
            time.sleep(.005)
    with tempfile.TemporaryDirectory(prefix='phase11-native-',dir=report.parent) as scratch:
        base=Path(scratch)
        source=base/'Video 日本.mkv'
        subprocess.run([resolve_tool('ffmpeg'),'-v','error','-f','lavfi','-i','testsrc2=size=160x90:rate=12:duration=1',
            '-f','lavfi','-i','sine=sample_rate=48000:duration=1','-c:v','libx264','-preset','ultrafast','-c:a','aac',str(source)],
            check=True,capture_output=True,timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        before=(hashlib.sha256(source.read_bytes()).hexdigest(),source.stat().st_ino,source.stat().st_mtime_ns)
        window=Workspace(QSettings(str(base/'settings.ini'),QSettings.Format.IniFormat))
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        window.show()
        def capture(stage):
            for theme in ('light','dark'):
                window.set_theme(theme,persist=False)
                app.processEvents()
                path=report.with_name(f'{report.stem}-{stage}-{theme}.png')
                assert window.grab().save(str(path))
                result['screenshots'][stage+'-'+theme]=str(path)
        try:
            window._tool_changed('Video')
            window.model.append([FileRecord.from_path(source)])
            window.jobs_button.click()
            queue=window.jobs_dialog
            assert not queue.isWindow() and window.workspace_stack.currentWidget() is queue
            editor=queue.workflow_editor
            editor.new()
            editor.use_workspace()
            editor.name.setText('Movie workflow')
            editor.tool.setCurrentText('Video')
            editor.configure()
            window.video_speed.setCurrentIndex(0)
            capture('configure-video')
            window.workflow_capture_button.click()
            editor.tool.setCurrentText('Rename')
            editor.configure()
            window.prefix.setText('Holiday_')
            window.detail.setCurrentIndex(window.detail.findData(2))
            window.workflow_capture_button.click()
            editor.tool.setCurrentText('Archives')
            editor.configure()
            window.format.setCurrentText('7z')
            window.archive_controls.name.setText('Holiday')
            window.workflow_capture_button.click()
            for width,height in ((1180,740),(880,610)):
                window.resize(width,height)
                capture(f'plan-{width}')
                result['layouts'].append(dict(requested=[width,height],actual=[window.width(),window.height()]))
                assert window.width()==width and window.height()==height
            editor.start_button.click()
            wait(lambda:queue.worker is not None)
            queue.refresh(queue.worker.job_id)
            queue.items_table.selectRow(0)
            window.resize(880,610)
            capture('running-880')
            wait(lambda:queue.worker is None and queue.plan_worker is None and not queue.enabled)
            assert queue.saved_jobs()[-1]['state']=='succeeded',queue.status.text()
            assert queue.jobs_model.rowCount()==1
            queue.items_table.selectRow(2)
            for width,height in ((1180,740),(880,610)):
                window.resize(width,height)
                capture(f'results-{width}')
                assert window.width()==width and window.height()==height
            job=queue.saved_jobs()[-1]['id']
            rows=queue.store.items(job)
            output=json.loads(rows[-1]['stage'])['outputs'][0]['source']
            renamed=json.loads(rows[1]['stage'])['outputs'][0]['source']
            extracted=subprocess.run([resolve_tool('7z'),'x','-so',output,Path(renamed).name],capture_output=True,check=True,timeout=20,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)).stdout
            assert extracted==Path(renamed).read_bytes()
            assert before==(hashlib.sha256(source.read_bytes()).hexdigest(),source.stat().st_ino,source.stat().st_mtime_ns)
            result.update(passed=True,embedded_queue=True,steps=[r['state'] for r in rows],
                original_hash_identity_mtime_preserved=True,archive_entry_matches_renamed_mp4=True,
                ordinary_queue_entries=queue.jobs_model.rowCount(),note='Native Windows source trial; no EXE rebuild or clean-machine/Linux claim.')
        except Exception as error:result['error']=repr(error)
        finally:
            window.close()
            wait(lambda:not window.busy and not window.isVisible())
            app.processEvents()
    result['fixture_removed']=not base.exists()
    report.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps({key:value for key,value in result.items() if key!='screenshots'},indent=2))
    return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
