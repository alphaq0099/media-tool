import json
from pathlib import Path
import threading
import time

from PIL import Image
from PySide6.QtTest import QTest

from test_foundation import app,window,wait_until
from test_images import fixture_image
from media_tool import images


def hold_conversion(monkeypatch):
    release=threading.Event()
    started=threading.Event()
    original=images.run_image
    def slow(*args,**kwargs):
        started.set()
        while not release.wait(.01):
            # UI assertions must never leave a blocked conversion thread.
            if time.monotonic()-begin>20:raise RuntimeError('Test release timed out')
        return original(*args,**kwargs)
    begin=time.monotonic()
    monkeypatch.setattr(images,'run_image',slow)
    return started,release


def start_image(window,tmp_path):
    source=fixture_image(tmp_path/'photo.png')
    window.model.append([source])
    window.format.setCurrentText('JPEG')
    (tmp_path/'output').mkdir()
    window.destination.setText(str(tmp_path/'output'))
    window.start_rename()
    wait_until(lambda:window.jobs_dialog is not None and window.jobs_dialog.worker is not None)
    return source,window.jobs_dialog


def test_results_owns_progress_and_navigation_preserves_active_job(window,tmp_path,monkeypatch):
    started,release=hold_conversion(monkeypatch)
    source,q=start_image(window,tmp_path)
    try:
        wait_until(started.is_set)
        assert window.tabs.currentIndex()==2
        assert window.results_table.isVisible()
        activity=window.visual.activity
        wait_until(lambda:source.name in activity.title.text())
        assert window.progress.parent() is activity.panel
        worker=q.worker
        window.tabs.setCurrentIndex(0)
        QTest.qWait(450)
        assert window.tabs.currentIndex()==0
        assert not window.progress.isVisible()
        window._tool_changed('Video')
        assert window.tabs.currentIndex()==0
        assert window.category_filter.currentText()=='All videos'
        assert window.proxy.rowCount()==0
        assert window.result_model.rowCount()==0
        QTest.qWait(220)
        assert window.tabs.currentIndex()==0
        window._tool_changed('Images')
        assert q.worker is worker
        assert window.tabs.currentIndex()==2
        assert window.format.currentText()=='JPEG'
        assert window.destination.text()==str(tmp_path/'output')
    finally:release.set()
    wait_until(lambda:q.worker is None)
    assert window.tabs.currentIndex()==2
    assert window.results_table.isVisible()
    assert not activity.timer.isActive()
    assert not activity.cancel.isVisible()
    assert activity.title.text()=='Succeeded'
    assert '1 completed' in activity.counts.text()
    results=q.store.items(window.last_result_job)
    assert results[0]['state']=='succeeded'
    output=Path(json.loads(results[0]['spec'])['output'])
    with Image.open(output) as image:assert image.size==(24,16)
    assert Path(source.path).exists()
    window._tool_changed('Video')
    window._tool_changed('Images')
    assert window.tabs.currentIndex()==0
    window.tabs.setCurrentIndex(2)
    assert window.result_model.items[0]['output']==str(output)


def test_completion_does_not_change_other_tool_or_tab(window,tmp_path,monkeypatch):
    started,release=hold_conversion(monkeypatch)
    source,q=start_image(window,tmp_path)
    try:
        wait_until(started.is_set)
        window._tool_changed('Rename')
        window.tabs.setCurrentIndex(1)
    finally:release.set()
    wait_until(lambda:q.worker is None)
    assert window.tool=='Rename'
    assert window.tabs.currentIndex()==1
    assert window.result_model.rowCount()==0
    window._tool_changed('Images')
    assert window.tabs.currentIndex()==0
    window.tabs.setCurrentIndex(2)
    assert window.result_model.items[0]['state']=='succeeded'


def test_all_tool_groups_and_fields_remain_accessible_at_minimum_size(window):
    window.resize(880,610)
    for _ in range(2):
        for tool in ('Video','Audio','Archives','Rename','Images','Documents'):
            window._tool_changed(tool)
            QTest.qWait(10)
            assert window.visual.groups['Output'].isVisible()
            assert window.settings_scroll.width()>=250
            assert window.start_rename_button.isVisible()
            assert window.start_rename_button.width()>=200
            assert not window.subtitle.isVisible()
            assert not window.phase_hint.isVisible()
            assert not window.original_cleanup_hint.isVisible()
            assert window.visual.heading_help.toolTip()
    window._tool_changed('Video')
    advanced=window.visual.groups['Advanced']
    assert not advanced.body.isVisible()
    advanced.header.click()
    assert advanced.body.isVisible()
    assert window.video_threads.isVisible()
    assert window.video_threads.isEnabled()


def test_workflow_results_follow_actual_child_without_switching_tools(window,tmp_path,monkeypatch):
    from test_workflows import enqueue,step
    started,release=hold_conversion(monkeypatch)
    source=fixture_image(tmp_path/'chain.png')
    window.model.append([source])
    queue=window.ensure_jobs()
    job=enqueue(queue.store,[source],[step('images',extension='jpg'),step('rename')])
    queue.start_single(job)
    try:
        wait_until(started.is_set)
        window._tool_changed('Images')
        assert window.tabs.currentIndex()==2
        binding=window.visual.activity.bindings['Images']
        assert binding['parent_id']==job
        assert binding['job_id'] and binding['job_id']!=job
        assert binding['step']==1 and binding['steps']==2
        window.tabs.setCurrentIndex(0)
        QTest.qWait(240)
        assert window.tabs.currentIndex()==0
        window._tool_changed('Video')
        assert window.tabs.currentIndex()==0
        window._tool_changed('Images')
        assert window.tabs.currentIndex()==2
        queue.show()
        queue.tabs.setCurrentWidget(queue.workflow_editor)
    finally:release.set()
    wait_until(lambda:queue.worker is None,15000)
    assert window.tool=='Images'
    assert window.tabs.currentIndex()==2
    assert window.result_model.items[0]['state']=='succeeded'
    assert Path(source.path).exists()
    assert queue.tabs.currentWidget() is queue.workflow_editor
