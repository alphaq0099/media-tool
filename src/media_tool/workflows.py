"""Durable tool chains. Child jobs use the ordinary protected adapters."""
from dataclasses import asdict,replace
import json
from pathlib import Path
import stat
import time

from .domain import FileRecord,path_key
from .jobs import Cancelled,check_source,checkpoint,hash_file,identity,regular,now
from .planning import PlanRequest

OPERATIONS={'video','images','audio','documents','rename','archives'}
LABELS=dict(video='Video',images='Images',audio='Audio',documents='Documents',rename='Rename',archives='Archive')


def check_flow(specs):
    kind=None
    for ordinal,spec in enumerate(specs,1):
        request=PlanRequest(**spec['request'])
        op=request.operation
        allowed={'video':{'video'},'images':{'image'},'audio':{'video','audio'}}.get(op)
        if op=='audio' and request.audio_action=='tags':allowed={'audio'}
        if op=='documents':
            allowed={'image'} if request.document_task=='images_pdf' else {'image','pdf'} if request.document_task=='ocr' else {'office'} if request.document_task=='office' else {'pdf'}
        if op=='archives' and request.archive_format=='cbz':allowed={'image'}
        if kind and allowed is not None and kind not in allowed:
            raise ValueError(f'Step {ordinal} ({LABELS[op]}) cannot use the preceding {kind} outputs. Change the step order or operation.')
        if op=='rename':continue
        kind={'video':'video','images':'image','audio':'audio','archives':'archive'}.get(op)
        if op=='documents':
            kind='image' if request.document_task=='render' else 'text' if request.document_task=='text' or request.document_task=='ocr' and request.extension=='txt' else 'pdf'


def validate_specs(specs):
    if not 1<=len(specs)<=20:
        raise ValueError('A workflow needs 1–20 steps.')
    if specs[0]['request']['operation']=='rename':
        raise ValueError('Start with a conversion or archive step. Rename can only change generated workflow files.')
    roots=specs[0].get('roots',[])
    if not 1<=len(roots)<=1000 or len({path_key(s['source']) for s in roots})!=len(roots):
        raise ValueError('Select 1–1000 distinct real input files for the workflow.')
    for root in roots:
        if not Path(root['source']).is_absolute():raise ValueError('Workflow inputs require absolute paths.')
    for spec in specs:
        request=PlanRequest(**spec['request'])
        if request.operation not in OPERATIONS or not request.keep_originals or request.collision!='unique':
            raise ValueError('Workflows keep originals and use unique output names.')
        if not spec.get('name'):raise ValueError('Each workflow step needs a name.')
        if request.operation=='archives' and request.archive_encrypt and not spec.get('archive_secret'):
            raise ValueError('Set an archive password before adding the encrypted step.')
    check_flow(specs)


def workflow_snapshot(records,steps,cancelled=lambda:False):
    roots=[]
    for record in sorted(records,key=lambda r:r.sort_key):
        checkpoint(cancelled)
        if record.sample or record.kind=='Folder':
            raise ValueError('Choose real files for workflow inputs; folder operations remain separate.')
        source=dict(source=record.path,identity=[record.device,record.inode,record.size,record.modified_ns])
        check_source(source)
        roots.append(source)
    specs=[]
    for index,step in enumerate(steps):
        request=replace(PlanRequest(**step['request']),keep_originals=True,collision='unique')
        anchor=None
        if request.destination.strip():
            destination=Path(request.destination).expanduser()
            if not destination.is_absolute():raise ValueError('Choose an absolute workflow output folder.')
            destination=destination.resolve(strict=False)
            request=replace(request,destination=str(destination))
            parent=destination
            while not parent.exists():parent=parent.parent
            info=parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or not info.st_ino:
                raise ValueError('Choose an ordinary workflow output folder with stable identities.')
            anchor=dict(path=str(parent),identity=identity(info)[:2])
        refs=[]
        for active,path in ((request.video_external_enabled,request.video_external_subtitle),
                            (request.audio_action=='tags' and request.audio_cover_mode=='replace',request.audio_cover_path)):
            if active:
                path=str(Path(path).expanduser().resolve(strict=True))
                refs.append(dict(source=path,identity=identity(regular(path))))
        specs.append(dict(name=f'{index+1}. {step["label"]}',request=asdict(request),
            roots=roots if index==0 else [],references=refs,destination_anchor=anchor,archive_secret=step.get('archive_secret','')))
    validate_specs(specs)
    return tuple(specs)


def inspect_artifacts(sources,cancelled):
    result=[]
    for source in sources:
        checkpoint(cancelled)
        check_source(source)
        digest=hash_file(source['source'],cancelled)
        check_source(source)
        if source.get('digest') and digest!=source['digest']:
            raise ValueError('A required workflow input/output changed. Dependent steps were stopped.')
        result.append(dict(source,digest=digest))
    return result


def plan_step(spec,inputs,cancelled):
    request=PlanRequest(**spec['request'])
    # Later steps receive generated files, not paths retained by an earlier UI
    # ordering dialog. Preserve the verified handoff order through renames.
    if not spec.get('roots'):
        order=tuple(s['source'] for s in inputs)
        if request.operation=='documents' and request.document_task in ('merge','images_pdf'):
            request=replace(request,document_order=order)
        if request.operation=='audio' and request.audio_action=='join':
            request=replace(request,audio_join_order=order)
    records=tuple(FileRecord.from_path(s['source']) for s in inputs)
    for source in spec.get('references',[]):check_source(source)
    if request.operation=='video':
        from .video import plan_video,video_snapshot
        planner,snapshot=plan_video,video_snapshot
    elif request.operation=='images':
        from .images import plan_images,image_snapshot
        planner,snapshot=plan_images,image_snapshot
    elif request.operation=='audio':
        from .audio import plan_audio,audio_snapshot
        planner,snapshot=plan_audio,audio_snapshot
    elif request.operation=='documents':
        from .documents import plan_documents,document_snapshot
        planner,snapshot=plan_documents,document_snapshot
    elif request.operation=='archives':
        from .zip_plan import plan_archives,archive_snapshot
        planner,snapshot=plan_archives,archive_snapshot
    else:
        from .renaming import plan_rename,rename_snapshot
        planner,snapshot=plan_rename,rename_snapshot
    plan=planner(records,request,cancelled)
    allowed={'Planned','Unchanged'} if request.operation=='rename' else {'Planned'}
    invalid=[item for item in plan.items if item.status not in allowed]
    if invalid or not plan.items:
        raise ValueError('Step could not be planned: '+('; '.join(i.source.name+': '+i.reason for i in invalid) or 'no outputs'))
    if request.operation=='rename' and not any(i.status=='Planned' for i in plan.items):
        specs=tuple(dict(source=i.source.path,output=i.source.path,name=i.source.name,
            identity=[i.source.device,i.source.inode,i.source.size,i.source.modified_ns],
            parent=str(Path(i.source.path).parent),parent_identity=i.parent_identity,
            initial_state='skipped',initial_message='Name unchanged.') for i in plan.items)
    else:specs=snapshot(plan)
    if request.operation=='archives':
        specs=tuple(dict(s,password_token=spec['archive_secret']) if s.get('encrypted') else s for s in specs)
    return request.operation,specs


def check_destination(spec):
    anchor=spec.get('destination_anchor')
    if anchor:
        path=Path(anchor['path'])
        info=path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or path.resolve(strict=True)!=path or
                identity(info)[:2]!=anchor['identity']):
            raise ValueError('Workflow output folder was replaced or moved. Restore it or queue a new plan.')


def guard_workflow_inputs(store,source,frozen,job_id,directory=False):
    """Do not rename/trash files reserved by an unrelated unfinished workflow."""
    owner=store.db.execute('SELECT parent_job FROM workflow_links WHERE child_job=?',(job_id,)).fetchone()
    family=owner[0] if owner else job_id
    key=path_key(source)
    for row in store.db.execute("""SELECT items.spec,items.stage FROM items JOIN jobs ON jobs.id=items.job_id
            WHERE jobs.adapter='workflow' AND jobs.id<>?
            AND jobs.state NOT IN ('succeeded','completed with skips','cancelled')""",(family,)):
        spec=json.loads(row['spec'])
        stage=json.loads(row['stage'] or '{}')
        artifacts=[*spec.get('roots',[]),*spec.get('references',[]),*stage.get('inputs',[]),*stage.get('outputs',[])]
        for artifact in artifacts:
            path=path_key(artifact['source'])
            same=path==key or bool(frozen and artifact.get('identity',[])[:2]==frozen[:2])
            inside=directory and Path(path).is_relative_to(Path(key))
            if same or inside:
                raise ValueError('Another unfinished workflow needs this file. Finish or cancel that workflow before changing its inputs.')


def rename_inputs(store,child,inputs,cancelled):
    """Locate only journaled identities through a partially committed rename."""
    expected={tuple(s['identity']):s for s in inputs}
    found=[]
    for item in store.items(child):
        spec=json.loads(item['spec'])
        source=expected.get(tuple(spec['identity']))
        if source is None:raise ValueError('Rename input is outside its recorded workflow outputs.')
        candidates=[]
        for name in dict.fromkeys((spec['source'],item['stage'],spec['output'])):
            if not name:continue
            try:
                if identity(regular(name))==source['identity']:candidates.append(name)
            except (OSError,ValueError):pass
        if len(candidates)!=1:
            raise ValueError('A generated file changed or is missing during rename recovery.')
        found.append(dict(source,source=candidates[0]))
    if len(found)!=len(inputs):raise ValueError('Rename no longer covers every required workflow output.')
    inspect_artifacts(found,cancelled)


def output_artifacts(store,child,operation,inputs,cancelled):
    expected={tuple(s['identity']):s['digest'] for s in inputs}
    outputs=[]
    for item in store.items(child):
        if item['state'] not in ('succeeded','skipped'):
            raise ValueError('Not every required file completed. Dependent steps were blocked.')
        spec=json.loads(item['spec'])
        output=spec['output']
        info=regular(output)
        if operation=='rename':
            if identity(info)!=spec['identity']:raise ValueError('Renamed output ownership changed.')
            digest=expected[tuple(spec['identity'])]
        else:
            if (str(info.st_dev),str(info.st_ino))!=(item['temp_dev'],item['temp_ino']) or not item['digest']:
                raise ValueError('Produced output ownership could not be verified.')
            digest=item['digest']
        outputs.append(dict(source=output,identity=identity(info),digest=digest))
    if not outputs:raise ValueError('Step produced no verified outputs.')
    return inspect_artifacts(outputs,cancelled)


def block_after(store,job_id,ordinal):
    for item in store.items(job_id):
        if item['ordinal']>ordinal and item['state']=='queued':
            store.set_item(item['id'],'blocked','Waiting for the preceding step to complete. Retry the workflow after resolving it.')


def execute_workflow(store,job_id,cancelled,paused,progress,hook=lambda point:None):
    from .jobs import execute_job
    outputs=None
    store.set_job(job_id,'running')
    for original in store.items(job_id):
        item=store.item(original['id'])
        spec=json.loads(item['spec'])
        stage=json.loads(item['stage'] or '{}')
        if item['state']=='succeeded':
            outputs=stage['outputs']
            continue
        if item['state']!='queued':
            block_after(store,job_id,item['ordinal'])
            return store.summarize(job_id)
        if paused():return store.summarize(job_id)
        try:
            checkpoint(cancelled)
            check_destination(spec)
            store.set_item(item['id'],'running','Inspecting workflow inputs',attempts=item['attempts']+1)
            inputs=stage.get('inputs') or outputs or spec['roots']
            child=stage.get('child_job')
            if child and spec['request']['operation']=='rename':
                rename_inputs(store,child,inputs,cancelled)
            else:inputs=inspect_artifacts(inputs,cancelled)
            if not child:
                operation,specs=plan_step(spec,inputs,cancelled)
                checkpoint(cancelled)
                with store.db:
                    child=store._enqueue(spec['name'],operation,specs)
                    stage=dict(inputs=inputs,child_job=child)
                    store.db.execute('INSERT INTO workflow_links VALUES (?,?,?)',(job_id,item['id'],child))
                    store.db.execute('UPDATE items SET stage=?,updated=? WHERE id=?',(json.dumps(stage),now(),item['id']))
                hook('workflow_planned')
            check_destination(spec)
            operation=spec['request']['operation']
            if any(i['state'] in ('failed','interrupted','cancelled','blocked') for i in store.items(child)):
                store.retry(child)
            store.set_item(item['id'],'running','Running '+spec['name'])
            run=store.begin_run(child)
            started=time.monotonic()
            try:
                execute_job(store,child,cancelled,paused,lambda _,done,total:progress(item['id'],done,total),hook)
            finally:
                store.end_run(run,time.monotonic()-started)
            child_items=store.items(child)
            if any(i['state']=='queued' for i in child_items) and paused():
                store.set_item(item['id'],'queued','Paused between files; completed work retained.')
                return store.summarize(job_id)
            if cancelled() or any(i['state']=='cancelled' for i in child_items):
                raise Cancelled('Workflow cancelled. Completed outputs retained.')
            hook('workflow_child_finished')
            outputs=output_artifacts(store,child,operation,inputs,cancelled)
            stage['outputs']=outputs
            if operation=='rename':
                renamed={tuple(s['identity']):s['source'] for s in outputs}
                stage['renames']=[dict(before=s['source'],after=renamed[tuple(s['identity'])],identity=s['identity']) for s in inputs]
            store.set_item(item['id'],'succeeded',f'{len(outputs)} verified output(s).',stage=json.dumps(stage))
            hook('workflow_checkpoint')
        except Exception as error:
            state='cancelled' if isinstance(error,Cancelled) else 'failed'
            store.set_item(item['id'],state,str(error))
            block_after(store,job_id,item['ordinal'])
            return store.summarize(job_id)
    return store.summarize(job_id)
