"""Support exports exclude all user-controlled strings and retain useful settings."""
from dataclasses import asdict
import json

import pytest
from PySide6.QtWidgets import QFileDialog
from test_foundation import app,window
from media_tool.jobs import Store
from media_tool.planning import PlanRequest
from media_tool.support_report import make_support_report,safe_settings
from media_tool.domain import FileRecord
from media_tool.workflows import workflow_snapshot


def test_support_report_excludes_names_paths_tokens_metadata_and_logs(tmp_path):
    store=Store(tmp_path/'history')
    private='PRIVATE_SENTINEL_日本_秘密'
    try:
        spec=dict(name=private,source=str(tmp_path/private),output='C:/Users/'+private+'/output.mp4',
            codec='hevc',container='mkv',crf=23,threads=2,keep_originals=True,
            password_token=private,metadata={'title':private},tools={'ffmpeg':private})
        job=store.enqueue(private,'video',[spec])
        store.set_item(store.items(job)[0]['id'],'failed',private+' packet error')
        report=make_support_report(store,job)
        text=json.dumps(report,ensure_ascii=False,allow_nan=False)
        assert private not in text and str(tmp_path) not in text and 'C:/Users' not in text
        assert report['job']['items'][0]['settings']==dict(codec='hevc',container='mkv',crf=23,threads=2,keep_originals=True)
        assert report['job']['counts']=={'failed':1}
        assert report['application_version']=='1.0.0'
        assert json.loads(store.items(job)[0]['spec'])['password_token']==private
    finally:store.close()


def test_workflow_support_settings_use_the_step_operation(tmp_path):
    store=Store(tmp_path/'history')
    try:
        source=tmp_path/'PRIVATE_TITLE.mkv';source.write_bytes(b'fixture')
        request=asdict(PlanRequest('video',extension='mkv',video_codec='hevc',video_mode='target_size'))
        request.update(prefix='PRIVATE_TITLE',destination=str(tmp_path/'PRIVATE_USER'),document_title='PRIVATE_TITLE')
        job=store.enqueue('PRIVATE_TITLE','workflow',workflow_snapshot([FileRecord.from_path(source)],[dict(label='Video',request=request)]))
        child=store.enqueue('PRIVATE_TITLE','video',[dict(name='PRIVATE_TITLE',codec='hevc',crf=18,password_token='PRIVATE_TOKEN')])
        store.set_item(store.items(job)[0]['id'],'running','PRIVATE_MESSAGE',stage=json.dumps(dict(child_job=child)))
        report=make_support_report(store,job)
        settings=report['job']['items'][0]['settings']
        assert settings['codec']=='hevc' and settings['mode']=='target_size'
        assert 'task' not in settings
        assert 'PRIVATE_' not in json.dumps(report)
        assert report['job']['items'][0]['files'][0]['settings']==dict(codec='hevc',crf=18)
    finally:store.close()


def test_unknown_strings_nonfinite_numbers_and_bools_are_not_leaked_as_settings():
    settings=safe_settings(dict(codec='/private/name',container=['private'],quality=True,duration=float('nan'),
        dpi=float('inf'),rotation=-1,threads=2,keep_originals='private',downsample=True))
    assert settings==dict(threads=2,downsample=True)


def test_unknown_job_has_clear_error(tmp_path):
    store=Store(tmp_path/'history')
    try:
        with pytest.raises(ValueError,match='existing job'):make_support_report(store,'missing')
    finally:store.close()


def test_ui_support_export_is_separate_from_detailed_report(window,tmp_path,monkeypatch):
    q=window.ensure_jobs()
    job=q.store.enqueue('PRIVATE_NAME','sample',[dict(name='PRIVATE_NAME',seconds=0)])
    q.refresh(job)
    target=tmp_path/'support.json'
    monkeypatch.setattr(QFileDialog,'getSaveFileName',lambda *a,**k:(str(target),'JSON'))
    q.export_support_report()
    assert 'PRIVATE_NAME' not in target.read_text()
    assert json.loads(target.read_text())['format']=='mediatool.support.v1'
    assert q.support_action.isEnabled() and q.export_action.isEnabled()
