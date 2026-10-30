"""Native source Queue soak: repeated real workflows and a 1,000-entry history."""
import argparse
from dataclasses import asdict
import gc
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import zipfile

import psutil

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from PIL import Image
from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtWidgets import QApplication
from media_tool import __version__
from media_tool.domain import FileRecord
from media_tool.planning import PlanRequest
from media_tool.window import Workspace
from media_tool.workflows import workflow_snapshot


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args()
    report=args.report.resolve()
    report.parent.mkdir(parents=True,exist_ok=True)
    app=QApplication([])
    app.setStyle('Fusion')
    process=psutil.Process()
    result=dict(version=__version__,passed=False,platform=app.platformName(),rounds=[])
    with tempfile.TemporaryDirectory(prefix='queue-soak-',dir=report.parent) as scratch:
        base=Path(scratch)
        records=[]
        originals={}
        for n in range(60):
            path=base/f'Input {n:03} 日本.png'
            Image.new('RGB',(256,192),(n*3%256,n*7%256,100)).save(path)
            records.append(FileRecord.from_path(path))
            originals[path]=(hashlib.sha256(path.read_bytes()).hexdigest(),path.stat().st_ino,path.stat().st_mtime_ns)
        window=Workspace(QSettings(str(base/'settings.ini'),QSettings.Format.IniFormat))
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        window.show()
        window.open_jobs()
        q=window.jobs_dialog
        timer=QTimer()
        timer.setInterval(20)
        ticks=[]
        timer.timeout.connect(lambda:ticks.append(time.monotonic()))
        timer.start()
        def wait(predicate,seconds=120):
            deadline=time.monotonic()+seconds
            while not predicate():
                if time.monotonic()>deadline:raise TimeoutError(q.status.text())
                app.processEvents()
                time.sleep(.002)
        try:
            for round_number in range(6):
                destination=base/f'round-{round_number}'
                destination.mkdir()
                steps=[dict(label=op.title(),request=asdict(PlanRequest(op,**settings))) for op,settings in (
                    ('images',dict(extension='jpg',destination=str(destination))),
                    ('rename',dict(prefix='Final_',padding=3)),
                    ('archives',dict(archive_name='Result',archive_format='zip')))]
                specs=workflow_snapshot(records,steps)
                job=q.store.enqueue(f'Batch {round_number+1}','workflow',specs)
                q.refresh(job)
                start=time.monotonic()
                q.start_single(job)
                wait(lambda:q.worker is None and not q.enabled)
                rows=q.store.items(job)
                assert all(i['state']=='succeeded' for i in rows),rows
                archive=json.loads(rows[-1]['stage'])['outputs'][0]['source']
                with zipfile.ZipFile(archive) as handle:
                    assert len(handle.namelist())==60
                    for name in handle.namelist():assert handle.read(name)==(destination/name).read_bytes()
                gc.collect()
                app.processEvents()
                info=process.memory_info()
                result['rounds'].append(dict(round=round_number+1,files=60,seconds=round(time.monotonic()-start,3),
                    rss_MiB=round(info.rss/2**20,2),private_MiB=round(getattr(info,'private',info.vms)/2**20,2),
                    handles=process.num_handles() if os.name=='nt' else process.num_fds(),
                    children=len(process.children(recursive=True))))
                print(json.dumps(result['rounds'][-1]),flush=True)
            # Idle history growth and filtering are UI operations, not media work.
            with q.store.db:
                for n in range(1000):q.store._enqueue(f'Waiting {n:04}','sample',[dict(name='Untouched',seconds=0)])
            start=time.monotonic()
            q.refresh()
            q.search.setText('Waiting 0999')
            app.processEvents()
            result['history_filter_seconds']=round(time.monotonic()-start,4)
            assert q.jobs_model.rowCount()==1
            q.search.clear()
            q.state_filter.setCurrentIndex(q.state_filter.findData('complete'))
            assert q.jobs_model.rowCount()==6
            cleared,protected=q.store.clear_completed()
            assert (cleared,protected)==(6,0)
            assert len(q.saved_jobs())==1000
            assert q.store.db.execute('SELECT COUNT(*) FROM workflow_links').fetchone()[0]==0
            for path,original in originals.items():
                assert (hashlib.sha256(path.read_bytes()).hexdigest(),path.stat().st_ino,path.stat().st_mtime_ns)==original
            timer.stop()
            rounds=result['rounds']
            growth=rounds[-1]['private_MiB']-rounds[0]['private_MiB']
            handle_growth=rounds[-1]['handles']-rounds[0]['handles']
            gaps=[b-a for a,b in zip(ticks,ticks[1:])]
            result.update(total_input_operations=360,verified_archive_entries=360,workflow_steps=18,
                max_event_loop_gap_seconds=round(max(gaps,default=0),4),event_loop_ticks=len(ticks),
                private_growth_MiB=round(growth,2),handle_growth=handle_growth,
                idle_cpu_percent_one_core=process.cpu_percent(interval=.5),
                no_child_tools_after_batches=not process.children(recursive=True),
                originals_preserved=True,history_clear_kept_files=True,
                note='Six 60-file image → rename → ZIP workflows; disk-full/disconnection are injected separately. Native Windows source only; not an endurance or physical USB-removal guarantee.')
            result['passed']=(growth<32 and handle_growth<30 and max(gaps,default=0)<1.0 and
                              result['history_filter_seconds']<1.0 and result['no_child_tools_after_batches'])
        except Exception as error:
            result['error']=repr(error)
        finally:
            timer.stop()
            window.close()
            wait(lambda:q.closed,seconds=20)
            app.processEvents()
    result['fixture_removed']=not base.exists()
    report.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2),flush=True)
    return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
