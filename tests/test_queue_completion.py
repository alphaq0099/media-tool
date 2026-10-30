import pytest

from test_foundation import app,window,wait_until
from test_queue_controls import page,sample


def test_estimate_appears_after_completed_files_and_idle_poll_does_not_read_history(window,monkeypatch):
    q=page(window)
    job=q.store.enqueue('Timed batch','sample',[dict(name=f'File {n}',seconds=.65) for n in range(4)])
    q.refresh(job)
    q.start_single(job)
    wait_until(lambda:'Estimated remaining' in q.progress_detail.text(),timeout=5000)
    assert 'files completed' in q.progress_detail.text()
    assert q.step_progress.isVisible()
    q.cancel_job()
    wait_until(lambda:q.worker is None)
    q.safe_refresh(force=True)
    reads=[]
    q.store.db.set_trace_callback(reads.append)
    try:
        q.safe_refresh()
        q.safe_refresh()
        assert reads==[]
    finally:q.store.db.set_trace_callback(None)


def test_starting_run_cannot_be_reordered_and_legacy_elapsed_is_unavailable(window):
    q=page(window)
    a,b=[sample(q.store,n) for n in ('A','B')]
    run=q.store.begin_run(a)
    with pytest.raises(ValueError,match='waiting'):q.store.move_waiting(a,1)
    q.store.end_run(run,.01)
    from media_tool.jobs import execute_job
    assert execute_job(q.store,b)=='succeeded'
    q.refresh(b)
    assert 'Elapsed unavailable' in q.progress_detail.text()
    assert q.jobs_model.rows[q.jobs_model.keys.index(b)][3]=='—'


def test_cancel_failed_workflow_releases_inputs_and_preserves_retry(window,tmp_path):
    from media_tool.jobs import execute_job
    from media_tool.planning import PlanRequest
    from media_tool.renaming import plan_rename,rename_snapshot
    from test_workflows import enqueue,image,step
    q=page(window)
    source=image(tmp_path/'source.png')
    workflow=enqueue(q.store,[source],[step('video',extension='mp4'),step('rename')])
    assert execute_job(q.store,workflow)=='failed'
    rename=q.store.enqueue('Rename','rename',rename_snapshot(plan_rename([source],PlanRequest('rename',prefix='Changed_'))))
    assert execute_job(q.store,rename)=='failed'
    q.refresh(workflow)
    assert q.cancel_button.isEnabled()
    q.cancel_job()
    assert [i['state'] for i in q.store.items(workflow)]==['cancelled','cancelled']
    q.store.retry(rename)
    assert execute_job(q.store,rename)=='succeeded'
    q.refresh(workflow)
    assert q.retry_button.isEnabled()


def test_parent_and_child_cancellation_is_atomic_and_retryable(tmp_path):
    import sqlite3
    from media_tool.jobs import Store,execute_job
    from test_workflows import enqueue,image,step
    store=Store(tmp_path/'history')
    try:
        source=image(tmp_path/'source.png')
        job=enqueue(store,[source],[step('images',extension='jpg'),step('archives')])
        def fail(point):
            if point=='written':raise OSError('Injected encoder failure')
        assert execute_job(store,job,hook=fail)=='failed'
        before=[(j,store.items(j['id'])) for j in store.jobs()]
        with store.db:
            store.db.execute("CREATE TRIGGER stop_cancel BEFORE UPDATE OF state ON jobs WHEN NEW.adapter='workflow' AND NEW.state='cancelled' BEGIN SELECT RAISE(ABORT,'Injected cancellation failure'); END")
        with pytest.raises(sqlite3.IntegrityError,match='Injected'):store.cancel_queued(job)
        assert [(j,store.items(j['id'])) for j in store.jobs()]==before
        with store.db:store.db.execute('DROP TRIGGER stop_cancel')
        store.cancel_queued(job)
        assert all(j['state']=='cancelled' for j in store.jobs())
        assert all(i['state']=='cancelled' for j in store.jobs() for i in store.items(j['id']))
        store.retry(job)
        assert execute_job(store,job)=='succeeded'
    finally:store.close()
