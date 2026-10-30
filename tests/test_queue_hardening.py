import json
from pathlib import Path
import sqlite3

import pytest

from media_tool.jobs import Store,execute_job
from test_foundation import app,window,wait_until
from test_workflows import enqueue,image,step
from test_queue_controls import page,sample


def test_reorder_waiting_while_another_job_finishes(window):
    q=page(window)
    a,b,c=[sample(q.store,n,.15) for n in ('A','B','C')]
    q.refresh(a)
    q.start_queue()
    wait_until(lambda:q.store.items(a)[0]['state']=='running')
    q.refresh(c)
    assert q.move_up_button.isEnabled()
    q.move_up_button.click()
    wait_until(lambda:q.worker is None and not q.enabled)
    assert [r[0] for r in q.store.db.execute('SELECT job_id FROM job_runs ORDER BY id')]==[a,c,b]


def test_clear_rollback_retains_complete_group_and_active_run(tmp_path):
    store=Store(tmp_path/'history')
    try:
        source=image(tmp_path/'source.png')
        job=enqueue(store,[source],[step('images',extension='jpg'),step('archives')])
        assert execute_job(store,job)=='succeeded'
        run=store.begin_run(job)
        assert store.clear_completed()==(0,1)
        store.end_run(run,1)
        before=[dict(j) for j in store.jobs()]
        with store.db:
            store.db.execute("CREATE TRIGGER deny_clear BEFORE DELETE ON jobs BEGIN SELECT RAISE(ABORT,'Injected history failure'); END")
        with pytest.raises(sqlite3.IntegrityError,match='Injected'):store.clear_completed()
        assert store.jobs()==before and len(store.items(job))==2
        assert store.db.execute('SELECT COUNT(*) FROM workflow_links').fetchone()[0]==2
        with store.db:store.db.execute('DROP TRIGGER deny_clear')
        assert store.clear_completed()==(1,0)
    finally:store.close()


@pytest.mark.parametrize('operation',['rename','trash','archive_cleanup','flatten'])
def test_other_operations_keep_unfinished_workflow_inputs(tmp_path,monkeypatch,operation):
    from media_tool.domain import FileRecord
    from media_tool.planning import PlanRequest
    from media_tool.images import plan_images,image_snapshot
    from media_tool.renaming import plan_rename,rename_snapshot
    from media_tool.zip_plan import plan_archives,archive_snapshot
    from media_tool.archive_cleanup import cleanup_archive
    from media_tool.folders import scan_folder
    from media_tool.folder_moves import plan_flatten
    root=tmp_path/'inputs'
    nested=root/'nested'
    nested.mkdir(parents=True)
    source=image(nested/'source.png')
    before=Path(source.path).read_bytes()
    store=Store(tmp_path/'history')
    def forbidden(*args):raise AssertionError('Protected source was sent to Trash')
    monkeypatch.setattr('media_tool.original_cleanup.move_to_trash',forbidden)
    monkeypatch.setattr('media_tool.archive_cleanup.move_to_trash',forbidden)
    try:
        workflow=enqueue(store,[source],[step('images',extension='jpg')])
        if operation=='rename':
            job=store.enqueue('Rename','rename',rename_snapshot(plan_rename([source],PlanRequest('rename',prefix='Changed_'))))
        elif operation=='trash':
            job=store.enqueue('Convert','images',image_snapshot(plan_images([source],PlanRequest('images',extension='jpg',keep_originals=False))))
        elif operation=='flatten':
            # Move a containing directory, not just the exact queued file path.
            deeper=nested/'inner'
            deeper.mkdir()
            Path(source.path).rename(deeper/'source.png')
            store.cancel_queued(workflow)
            source=FileRecord.from_path(deeper/'source.png')
            destination=tmp_path/'rendered'
            destination.mkdir()
            workflow=enqueue(store,[source],[step('images',extension='jpg',destination=str(destination))])
            plan=plan_flatten(scan_folder(root))
            assert plan.can_apply
            job=store.enqueue('Flatten','flatten',plan.specs)
        else:
            job=store.enqueue('ZIP','archives',archive_snapshot(plan_archives([source],PlanRequest('archives'))))
        state=execute_job(store,job)
        if operation in ('rename','flatten'):
            assert state=='failed'
            assert 'unfinished workflow' in store.items(job)[0]['message']
        else:
            assert state=='succeeded'
            if operation=='archive_cleanup':cleanup_archive(store,store.items(job)[0]['id'])
            assert 'unfinished workflow' in json.dumps(store.items(job)[0])
        assert Path(source.path).read_bytes()==before
        assert store.items(workflow)[0]['state']=='queued'
        assert execute_job(store,workflow)=='succeeded'
        if operation in ('rename','flatten'):
            store.retry(job)
            assert execute_job(store,job)=='succeeded'
    finally:store.close()


def test_destination_disconnect_during_write_keeps_owned_temp_for_retry(tmp_path):
    store=Store(tmp_path/'history')
    destination=tmp_path/'output'
    destination.mkdir()
    source=image(tmp_path/'input.png')
    job=enqueue(store,[source],[step('images',extension='jpg',destination=str(destination)),step('archives')])
    try:
        def disconnect(point):
            if point=='written':destination.rename(tmp_path/'offline')
        assert execute_job(store,job,hook=disconnect)=='failed'
        assert [i['state'] for i in store.items(job)]==['failed','blocked']
        assert len(list((tmp_path/'offline').iterdir()))==1
        (tmp_path/'offline').rename(destination)
        store.retry(job)
        assert execute_job(store,job)=='succeeded'
        assert not list(destination.glob('*.part'))
        assert len(list(destination.glob('*.jpg')))==1
        assert Path(source.path).exists()
    finally:store.close()


def test_workflow_progress_is_step_weighted_with_file_detail(window,tmp_path):
    q=page(window)
    source=image(tmp_path/'source.png')
    job=enqueue(q.store,[source],[step('images',extension='jpg'),step('archives')])
    assert execute_job(q.store,job)=='succeeded'
    rows=q.store.items(job)
    second=rows[1]
    # A persisted failed second step must not display a fully completed workflow.
    q.store.set_item(second['id'],'failed','Injected failure')
    q.store.summarize(job)
    q.refresh(job)
    assert q.progress.value()==50
    assert '1/2 steps completed' in q.progress_detail.text()
    assert 'ETA unavailable' in q.progress_detail.text()
    assert 'Elapsed' in q.items_model.rows[0][3]
