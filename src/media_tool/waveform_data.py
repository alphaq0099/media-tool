"""Bounded waveform peaks from the existing supervised FFmpeg, not full PCM."""
import math


def extract(record, track=0, cancelled=lambda:False):
    from .jobs import check_source
    from .audio import inspect_tools,audio_info,packet_duration
    from .processes import run_process
    spec=dict(source=record.path,identity=[record.device,record.inode,record.size,record.modified_ns])
    check_source(spec)
    tools=inspect_tools('wav',True,cancelled)
    info=audio_info(record.path,tools,cancelled)
    streams=info['streams']
    if not streams or not 0<=track<len(streams):raise ValueError('The selected audio track is unavailable.')
    stream=streams[track]
    duration=stream.get('duration')
    if not duration or duration<=0:duration=packet_duration(record.path,stream,tools,cancelled)
    if not duration or not math.isfinite(duration) or not 0<duration<=7*86400:
        raise ValueError('A known audio duration of up to seven days is required for the waveform.')
    bins=1200
    # At most four seconds of downsampled mono per statistics frame; 1,200
    # retained bins regardless of track length. No PCM accumulates in Python.
    step=min(32000,max(80,math.ceil(duration*8000/bins)))
    peaks=[0.]*bins
    timestamp=0.
    count=0
    def line(value):
        nonlocal timestamp,count
        if value.startswith('frame:') and 'pts_time:' in value:
            timestamp=float(value.split('pts_time:',1)[1].split()[0])
        elif value.startswith('lavfi.astats.Overall.Peak_level='):
            number=float(value.split('=',1)[1])
            level=0. if not math.isfinite(number) else min(1.,10**(number/20))
            first=max(0,min(bins-1,int(timestamp/duration*bins)))
            last=max(first,min(bins-1,int((timestamp+step/8000)/duration*bins)))
            for index in range(first,last+1):peaks[index]=max(peaks[index],level)
            count+=1
    filters=(f'aresample=8000,asetpts=PTS-STARTPTS,asetnsamples=n={step}:p=0,'
        'astats=metadata=1:reset=1:measure_perchannel=none:measure_overall=Peak_level,'
        'ametadata=mode=print:key=lavfi.astats.Overall.Peak_level:file=-')
    run_process([tools['ffmpeg'],'-hide_banner','-v','error','-nostdin','-protocol_whitelist','file,pipe',
        '-i',record.path,'-map',f"0:{stream['index']}",'-vn','-ac','1','-af',filters,'-threads','1','-f','null','-'],
        cancelled,timeout=180,on_line=line)
    check_source(spec)
    if not count:raise ValueError('FFmpeg returned no waveform samples.')
    return dict(peaks=peaks,duration=duration,tracks=[dict(index=s['index'],codec=s['codec_name'],
        channels=s.get('channels'),rate=s.get('sample_rate')) for s in streams],track=track)
