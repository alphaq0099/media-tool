import errno
import json
from pathlib import Path
import sys

import pytest
from PySide6.QtWidgets import QMessageBox

from media_tool.domain import FileRecord
from media_tool.jobs import Store, SCHEMA, copy_snapshot, execute_job
from media_tool.jobs_ui import QueuePage
from test_foundation import app, window, wait_until
from test_workflows import step, image, enqueue


def sample(store, title='Sample', seconds=0):
    return store.enqueue(title,'sample',[dict(name=title,seconds=seconds)])


def page(window):
    window.open_jobs()
    window.jobs_dialog.tabs.setCurrentWidget(window.jobs_dialog.views.history)
    return window.jobs_dialog


def test_order_survives_reopen_and_only_moves_waiting(tmp_path):
    store=Store(tmp_path/'history')
    a,b,c=[sample(store,n) for n in ('A','B','C')]
    store.set_job(a,'running')
    assert store.move_waiting(c,-1)
    with pytest.raises(ValueError,match='waiting'):store.move_waiting(a,1)
    store.close()
    store=Store(tmp_path/'history')
    try:
        assert [j['id'] for j in store.visible_jobs()]==[a,c,b]
        assert not store.move_waiting(c,-1)
        assert store.move_waiting(c,1)
    finally:store.close()


def test_filters_do_not_change_scheduling_and_order(window):
    q=page(window)
    a,b,c=[sample(q.store,n,.06) for n in ('Alpha','Beta','Gamma')]
    q.refresh(c)
    q.move_selected(-1)
    q.search.setText('Alpha')
    assert q.jobs_model.keys==[a]
    q.state_filter.setCurrentIndex(q.state_filter.findData('queued'))
    q.start_queue()
    wait_until(lambda:q.worker is None and not q.enabled)
    assert q.jobs_model.keys==[]
    starts=list(q.store.db.execute("SELECT job_id FROM job_runs ORDER BY id"))
    assert [r[0] for r in starts]==[a,c,b]
    q.search.clear()
    q.state_filter.setCurrentIndex(q.state_filter.findData('complete'))
    assert q.jobs_model.keys==[a,c,b]


def test_clear_completed_workflow_is_atomic_and_keeps_every_file(tmp_path):
    store=Store(tmp_path/'history')
    source=image(tmp_path/'input.png')
    job=enqueue(store,[source],[step('images',extension='jpg'),step('rename'),step('archives')])
    try:
        assert execute_job(store,job)=='succeeded'
        before={str(p):p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
        assert store.clear_completed(preview=True)==(1,0)
        assert len(store.jobs())==4
        assert store.clear_completed()==(1,0)
        assert store.jobs()==[]
        for table in ('workflow_links','items','events','queue_order','folder_cleanup','job_runs'):
            assert store.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]==0
        assert before=={str(p):p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    finally:store.close()


def test_clear_keeps_undo_and_active_or_incomplete_groups(tmp_path):
    store=Store(tmp_path/'history')
    try:
        rename=store.enqueue('Rename','rename',[dict(name='a',parent=str(tmp_path))])
        store.set_item(store.items(rename)[0]['id'],'succeeded')
        store.summarize(rename)
        undo=sample(store,'Undo')
        with store.db:store.db.execute('INSERT INTO rename_undos VALUES (?,?)',(rename,undo))
        execute_job(store,undo)
        waiting=sample(store,'Waiting')
        failed=sample(store,'Failed')
        store.set_item(store.items(failed)[0]['id'],'failed')
        store.summarize(failed)
        done=sample(store,'Done')
        execute_job(store,done)
        assert store.clear_completed()==(1,2)
        assert {j['id'] for j in store.jobs()}=={rename,undo,waiting,failed}
        assert store.undo_job(rename)==undo
    finally:store.close()


def test_clear_while_active_and_hidden_filter(window,monkeypatch):
    q=page(window)
    done=sample(q.store,'Done')
    execute_job(q.store,done)
    active=sample(q.store,'Running',.5)
    q.refresh(active)
    q.start_single(active)
    wait_until(lambda:q.store.items(active)[0]['state']=='running')
    q.search.setText('Running')
    monkeypatch.setattr(QMessageBox,'question',lambda *args:QMessageBox.StandardButton.Yes)
    q.clear_history()
    assert done not in {j['id'] for j in q.store.jobs()}
    assert q.worker is not None
    wait_until(lambda:q.worker is None)
    assert q.store.items(active)[0]['state']=='succeeded'


def test_run_time_excludes_waiting_and_survives_restart(tmp_path):
    store=Store(tmp_path/'history')
    job=sample(store)
    run=store.begin_run(job)
    store.end_run(run,2.5)
    unfinished=store.begin_run(job)
    with store.db:
        store.db.execute("UPDATE job_runs SET started='2020-01-01T00:00:00+00:00' WHERE id=?",(unfinished,))
        store.db.execute("UPDATE events SET time='2020-01-01T00:00:04+00:00' WHERE job_id=?",(job,))
    store.close()
    store=Store(tmp_path/'history')
    try:
        store.recover()
        assert store.elapsed_times()[job]==6.5
        store.recover()
        assert store.elapsed_times()[job]==6.5
    finally:store.close()


def test_schema21_migration_retains_jobs(tmp_path):
    directory=tmp_path/'history'
    store=Store(directory)
    job=sample(store)
    with store.db:
        store.db.execute('DROP TABLE queue_order')
        store.db.execute('DROP TABLE job_runs')
        store.db.execute('PRAGMA user_version=21')
    store.close()
    store=Store(directory)
    try:
        assert store.visible_jobs()[0]['id']==job
        assert store.db.execute('PRAGMA user_version').fetchone()[0]==SCHEMA
        assert execute_job(store,job)=='succeeded'
    finally:store.close()


def test_progress_elapsed_and_retry_scope(window):
    q=page(window)
    job=q.store.enqueue('Batch','sample',[dict(name=str(n),seconds=.08,fail_once=n==3) for n in range(4)])
    q.refresh(job)
    q.start_single(job)
    wait_until(lambda:q.live_progress is not None)
    q.update_progress()
    assert 'Elapsed' in q.progress_detail.text() and 'files' in q.progress_detail.text()
    wait_until(lambda:q.worker is None)
    assert q.progress.value()==75
    assert q.store.elapsed_times()[job]>.1
    assert '1 incomplete files' in q.retry_button.toolTip()
    q.retry_job()
    q.start_single(job)
    wait_until(lambda:q.worker is None)
    assert q.progress.value()==100
    assert [i['attempts'] for i in q.store.items(job)]==[1,1,1,2]


@pytest.mark.parametrize('failure',['disk_full','disconnected','replaced'])
def test_workflow_destination_failures_block_and_recover(tmp_path,monkeypatch,failure):
    import media_tool.jobs as jobs
    store=Store(tmp_path/'history')
    destination=tmp_path/'output'
    destination.mkdir()
    source=image(tmp_path/'input.png')
    before=Path(source.path).read_bytes()
    job=enqueue(store,[source],[step('images',extension='jpg',destination=str(destination)),step('archives')])
    original_commit=jobs.commit_no_replace
    try:
        if failure=='disk_full':
            def full(*args):raise OSError(errno.ENOSPC,'Injected full destination')
            monkeypatch.setattr(jobs,'commit_no_replace',full)
            # Images imports commit_no_replace directly.
            monkeypatch.setattr('media_tool.images.commit_no_replace',full)
        else:
            destination.rename(tmp_path/'disconnected output')
            if failure=='replaced':destination.mkdir()
        assert execute_job(store,job)=='failed'
        assert [i['state'] for i in store.items(job)]==['failed','blocked']
        assert Path(source.path).read_bytes()==before
        assert not list(tmp_path.glob('Archive*.zip'))
        assert store.clear_completed()[0]==0
        if failure=='disk_full':
            monkeypatch.setattr(jobs,'commit_no_replace',original_commit)
            monkeypatch.setattr('media_tool.images.commit_no_replace',original_commit)
        else:
            if destination.exists():destination.rmdir()
            (tmp_path/'disconnected output').rename(destination)
        store.retry(job)
        assert execute_job(store,job)=='succeeded',store.items(job)
    finally:store.close()


def test_shutdown_terminates_external_process_tree_and_keeps_waiting(window,tmp_path,monkeypatch):
    import psutil
    from media_tool.processes import run_process
    q=page(window)
    source=tmp_path/'input.txt'
    source.write_text('Original')
    output=tmp_path/'output'
    output.mkdir()
    job=q.store.enqueue('External work','copy',copy_snapshot([FileRecord.from_path(source)],output))
    waiting=sample(q.store,'Waiting')
    marker=tmp_path/'child-pid.txt'
    code="import os,time,pathlib; pathlib.Path("+repr(str(marker))+").write_text(str(os.getpid())); time.sleep(90)"
    def slow(store,item,cancelled,progress,hook):
        run_process([getattr(sys,'_base_executable',sys.executable),'-c',code],cancelled=cancelled)
    monkeypatch.setattr('media_tool.jobs.run_copy',slow)
    q.refresh(job)
    q.start_queue()
    wait_until(marker.exists,timeout=15000)
    pid=int(marker.read_text())
    assert psutil.pid_exists(pid)
    window.close()
    wait_until(lambda:q.closed,timeout=15000)
    wait_until(lambda:not psutil.pid_exists(pid),timeout=5000)
    reopened=Store(q.directory)
    try:
        assert reopened.items(job)[0]['state']=='cancelled'
        assert reopened.items(waiting)[0]['state']=='queued'
        assert source.read_text()=='Original' and list(output.iterdir())==[]
    finally:reopened.close()
