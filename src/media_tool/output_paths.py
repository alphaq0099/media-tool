"""Locate generated outputs through workflow renames and report shell failures."""
import json
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices


def resolved_output(store,job_id,path):
    if not path or store is None or not job_id:return path
    link=store.db.execute('SELECT parent_job FROM workflow_links WHERE child_job=?',(job_id,)).fetchone()
    parent=link[0] if link else job_id
    for item in store.items(parent):
        try:stage=json.loads(item['stage'] or '{}')
        except ValueError:continue  # Ordinary rename stages contain paths.
        if not isinstance(stage,dict):continue
        for rename in stage.get('renames',[]):
            if Path(path)==Path(rename['before']):path=rename['after']
    return path


def job_output_paths(store,job_id,selected=None):
    items=store.items(job_id) if job_id else []
    ordered=[i for i in items if i['id']==selected]+[i for i in reversed(items) if i['id']!=selected]
    paths=[]
    for item in ordered:
        spec=json.loads(item['spec'])
        if 'request' in spec:
            stage=json.loads(item['stage'] or '{}')
            candidates=[s['source'] for s in stage.get('outputs',[])]
        else:
            candidates=[spec.get('output')] if item['state']=='succeeded' else []
        for path in candidates:
            path=resolved_output(store,job_id,path)
            if path and path not in paths:paths.append(path)
    return paths


def open_output_path(path,notify,folder=False):
    if not path:
        notify('No completed output is available.')
        return False
    path=Path(path).absolute()
    try:
        exists=path.exists()
        target=path.parent if folder else path
        if exists and target.exists() and QDesktopServices.openUrl(QUrl.fromLocalFile(str(target))):
            return True
        if path.parent.is_dir() and QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent))):
            notify('No default app opened this output. Opened its folder.' if exists else
                   'The saved output has moved or been removed. Opened its folder.')
            return True
        notify('Could not open the output. Check its saved path and default app: '+str(path))
    except (OSError,RuntimeError) as error:
        notify('Could not open the output: '+str(error))
    return False
