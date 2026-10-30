"""Native audio/document UI checks; all inputs and history are disposable."""
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
from PySide6.QtCore import QSettings,Qt,QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication,QDialog,QInputDialog
from media_tool import __version__
from media_tool.domain import FileRecord
from media_tool.window import Workspace


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--report',type=Path,required=True)
    report=parser.parse_args().report.resolve()
    report.parent.mkdir(parents=True,exist_ok=True)
    app=QApplication([])
    app.setStyle('Fusion')
    app.setFont(QFont('Segoe UI',10))
    result=dict(version=__version__,passed=False,qt_platform=app.platformName(),screenshots={},jobs=[],layouts=[])
    def wait(predicate,seconds=70):
        deadline=time.monotonic()+seconds
        while not predicate():
            if time.monotonic()>deadline:raise TimeoutError('Native audio/document trial timed out')
            app.processEvents()
            time.sleep(.005)
    with tempfile.TemporaryDirectory(prefix='phase9-10-native-',dir=report.parent) as scratch:
        base=Path(scratch)
        source=base/'Sound 日本.wav'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=3',
            '-c:a','pcm_s24le',str(source)],check=True,capture_output=True,timeout=30,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        from pypdf import PdfWriter
        document=base/'Pages 日本.pdf'
        writer=PdfWriter()
        for width in (200,220,240):writer.add_blank_page(width,300)
        writer.add_metadata({'/Title':'Native fixture'})
        writer.write(document)
        writer.close()
        from PIL import Image
        cover=base/'Cover.png'
        Image.new('RGB',(180,120),'#2c9da8').save(cover)
        sources=[source,document,cover]
        def originals():
            return [(hashlib.sha256(p.read_bytes()).hexdigest(),p.stat().st_ino,p.stat().st_mtime_ns) for p in sources]
        before=originals()
        window=Workspace(QSettings(str(base/'settings.ini'),QSettings.Format.IniFormat))
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        window.show()
        def capture(stage):
            app.processEvents()
            path=report.with_name(f'{report.stem}-{stage}.png')
            assert window.grab().save(str(path))
            result['screenshots'][stage]=str(path)
        def run(stage,records):
            window.model.clear()
            window.model.append([FileRecord.from_path(p) for p in records])
            window.preview_button.click()
            wait(lambda:not window.busy)
            assert window.last_plan and all(i.status=='Planned' for i in window.last_plan.items),window.status.text()
            capture(stage+'-preview')
            outputs=[Path(i.output) for i in window.last_plan.items]
            window.start_rename_button.click()
            wait(lambda:not window.busy and window.tabs.currentIndex()==2)
            assert all(i['state']=='succeeded' for i in window.result_model.items),window.result_model.items
            result['jobs'].append(dict(task=stage,succeeded=len(outputs)))
            capture(stage+'-results')
            return outputs
        try:
            # Every new task, two themes and ordinary/narrow workspace sizes.
            for width,height in ((1180,740),(880,610)):
                window.resize(width,height)
                for theme in ('light','dark'):
                    window.set_theme(theme,persist=False)
                    window._tool_changed('Audio')
                    audio=window.audio_controls
                    for task in ('volume','normalize','tags'):
                        audio.task.setCurrentIndex(audio.task.findData(task))
                        if task=='tags':audio.tag_mode.setCurrentIndex(audio.tag_mode.findData('edit'))
                        window.settings_scroll.verticalScrollBar().setValue(0)
                        capture(f'audio-{task}-{width}-{theme}')
                        window.settings_scroll.verticalScrollBar().setValue(window.settings_scroll.verticalScrollBar().maximum())
                        capture(f'audio-{task}-options-{width}-{theme}')
                    window._tool_changed('Documents')
                    controls=window.document_controls
                    for index in range(controls.task.count()):
                        controls.task.setCurrentIndex(index)
                        if controls.task.currentData()=='protect':
                            controls.protection.setCurrentIndex(controls.protection.findData('set'))
                        window.settings_scroll.verticalScrollBar().setValue(0)
                        capture(f'document-{controls.task.currentData()}-{width}-{theme}')
                        if controls.task.currentData() in ('protect','metadata','optimize','ocr'):
                            window.settings_scroll.verticalScrollBar().setValue(window.settings_scroll.verticalScrollBar().maximum())
                            capture(f'document-{controls.task.currentData()}-options-{width}-{theme}')
                    app.processEvents()
                    assert window.width()==width and window.height()==height
                    assert window.theme_button.isVisible()
                    assert window.theme_button.geometry().bottom()<window.theme_button.parentWidget().height()
                result['layouts'].append([width,height])
            window.resize(1180,740)
            window._tool_changed('Audio')
            window.format.setCurrentText('WAV')
            audio.task.setCurrentIndex(audio.task.findData('volume'))
            audio.gain.setValue(-3)
            run('volume',[source])
            audio.task.setCurrentIndex(audio.task.findData('normalize'))
            run('normalize',[source])
            audio.task.setCurrentIndex(audio.task.findData('tags'))
            audio.tag_fields['title'].setText('Song 日本')
            audio.cover_mode.setCurrentIndex(audio.cover_mode.findData('replace'))
            audio.cover_path.setText(str(cover))
            run('tags',[source])
            window._tool_changed('Documents')
            controls.task.setCurrentIndex(controls.task.findData('extract'))
            controls.pages.setText('3,1')
            run('extract',[document])
            controls.task.setCurrentIndex(controls.task.findData('images_pdf'))
            images_pdf=run('images-pdf',[cover])[0]
            controls.task.setCurrentIndex(controls.task.findData('render'))
            controls.pages.clear()
            run('render',[images_pdf])
            controls.task.setCurrentIndex(controls.task.findData('protect'))
            controls.output_password.setText('Native fixture 日本')
            controls.confirm.setText('Native fixture 日本')
            run('password',[document])
            queue=window.jobs_dialog
            queue.refresh(window.last_result_job)
            queue.show_archive_results()
            assert window.tool=='Documents' and window.tabs.currentIndex()==2
            # Real asynchronous PDFium thumbnail, then close the modal preview.
            window.model.append([FileRecord.from_path(document)])
            controls.task.setCurrentIndex(controls.task.findData('extract'))
            window.preview_button.click()
            wait(lambda:not window.busy)
            original_get_int=QInputDialog.getInt
            QInputDialog.getInt=lambda *a,**k:(1,True)
            thumbnail_seen=[]
            def dismiss():
                dialog=QApplication.activeModalWidget()
                if isinstance(dialog,QDialog) and dialog.windowTitle()=='PDF page preview':
                    thumbnail_seen.append(True)
                    path=report.with_name(report.stem+'-thumbnail.png')
                    dialog.grab().save(str(path))
                    result['screenshots']['thumbnail']=str(path)
                    dialog.accept()
                else:QTimer.singleShot(50,dismiss)
            QTimer.singleShot(50,dismiss)
            controls.show_page_preview()
            wait(lambda:controls.worker is None)
            QInputDialog.getInt=original_get_int
            assert thumbnail_seen
            assert originals()==before
            result.update(passed=True,originals_hash_identity_mtime_preserved=True,
                note='Native Windows source trial. Optional external engines not installed; no real OCR/Office output claim.')
        except Exception as error:result['error']=repr(error)
        finally:
            window.close()
            wait(lambda:not window.busy and not window.isVisible())
            app.processEvents()
    result['fixture_removed']=not base.exists()
    report.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='screenshots'},indent=2))
    return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
