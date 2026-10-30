"""Allowlisted support diagnostics. User-controlled text is never exported."""
from collections import Counter
import json
import math
import platform
import sys

OPERATIONS={'images','video','audio','documents','rename','archives','flatten','workflow','sample','copy'}
STATES={'queued','running','verifying','committing','succeeded','skipped','failed','interrupted','cancelled','blocked','completed with skips'}
ENUMS={
    'codec':{'h264','hevc','vp9','aac','opus','mp3','flac','pcm_s16le','pcm_s24le'},
    'container':{'mp4','mkv','webm'},
    'extension':{'jpg','jpeg','png','webp','mp4','mkv','webm','mp3','m4a','aac','flac','wav','opus','ogg','pdf','txt','zip','7z','cbz'},
    'task':{'merge','split','extract','reorder','remove','rotate','images_pdf','render','text','optimize','metadata','protect','ocr','office'},
    'mode':{'quality','target_size','copy','convert','combined','separate'},
    'preset':{'medium','veryfast','superfast'},
    'compression':{'lossless','jpeg'},
    'archive_format':{'zip','7z','cbz'},
    'action':{'convert','remux','trim','split','join','normalize','volume','tags'},
}
NUMBERS={'duration','quality','crf','threads','dpi','image_dpi','rotation','page_count','track','level'}
BOOLEANS={'keep_originals','downsample','encrypted','no_upscale'}
REQUEST_KEYS={
    'video':{'video_codec':'codec','video_action':'action','video_preset':'preset','video_mode':'mode','video_crf':'crf','video_threads':'threads'},
    'audio':{'audio_action':'action','audio_mode':'mode'},
    'documents':{'document_task':'task','document_compression':'compression','document_dpi':'dpi','document_quality':'quality','document_downsample':'downsample'},
    'images':{'image_quality':'quality'},
    'archives':{'archive_format':'archive_format','archive_level':'level'},
}


def safe_settings(spec):
    result={}
    for key,allowed in ENUMS.items():
        value=spec.get(key)
        if isinstance(value,str) and value in allowed:result[key]=value
    for key in NUMBERS:
        value=spec.get(key)
        if type(value) in (int,float) and 0<=value<=10**12 and math.isfinite(value):result[key]=value
    for key in BOOLEANS:
        if type(spec.get(key)) is bool:result[key]=spec[key]
    return result


def make_support_report(store,job_id):
    from PySide6 import __version__ as binding_version
    from PySide6.QtCore import qVersion
    from . import __version__
    job=store.db.execute('SELECT adapter,state FROM jobs WHERE id=?',(job_id,)).fetchone()
    if job is None:raise ValueError('Select an existing job before exporting a support report.')
    def items_for(key,depth=0):
        result=[]
        for number,item in enumerate(store.items(key),1):
            spec=json.loads(item['spec'])
            row=dict(number=number,state=item['state'] if item['state'] in STATES else 'unknown',
                attempts=item['attempts'] if type(item['attempts']) is int else 0)
            if 'request' in spec:
                request=spec['request']
                operation=request.get('operation')
                row['operation']=operation if isinstance(operation,str) and operation in OPERATIONS else 'unknown'
                mapping=REQUEST_KEYS.get(row['operation'],{})
                converted={mapping[k]:request[k] for k in mapping if k in request}
                converted.update({k:request[k] for k in ('extension','keep_originals') if k in request})
                row['settings']=safe_settings(converted)
                if depth==0:
                    stage=json.loads(item['stage'] or '{}')
                    child=stage.get('child_job')
                    if child:row['files']=items_for(child,depth+1)
            else:row['settings']=safe_settings(spec)
            result.append(row)
        return result
    items=items_for(job_id)
    return dict(format='mediatool.support.v1',application_version=__version__,
        runtime=dict(python='.'.join(map(str,sys.version_info[:3])),qt=qVersion(),pyside=binding_version,
                     system=platform.system(),release=platform.release(),machine=platform.machine(),frozen=bool(getattr(sys,'frozen',False))),
        job=dict(operation=job['adapter'] if job['adapter'] in OPERATIONS else 'unknown',
                 state=job['state'] if job['state'] in STATES else 'unknown',counts=dict(Counter(i['state'] for i in items)),items=items),
        excluded=['filenames','paths','passwords','password tokens','titles','tags','metadata','free-form messages','timestamps'])
