"""Sample a bounded set of local frames; the decoder can also run in an isolated child."""
import base64
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import zlib

MAX_FRAMES=3
MAX_SOURCE=50*1024*1024
MAX_PIXELS=4096*2304


def png(frame):
    scale=min(1,512/frame.width,384/frame.height)
    rgb=frame.reformat(width=max(1,int(frame.width*scale)),height=max(1,int(frame.height*scale)),format='rgb24')
    plane=rgb.planes[0]
    pixels=bytes(plane)
    raw=b''.join(b'\0'+pixels[i*plane.line_size:i*plane.line_size+rgb.width*3] for i in range(rgb.height))
    def chunk(kind,value):
        return struct.pack('>I',len(value))+kind+value+struct.pack('>I',zlib.crc32(kind+value))
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',rgb.width,rgb.height,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(raw))+chunk(b'IEND',b'')


def decode(path,count):
    import av
    if not 0<count<=MAX_FRAMES or path.stat().st_size>MAX_SOURCE:
        return []
    with path.open('rb') as stream:
        if stream.read(12)[4:8]!=b'ftyp':
            return []
        stream.seek(0)
        # Accept the local MP4 container only, without external data references.
        with av.open(stream,format='mov',options={'enable_drefs':'0','use_absolute_path':'0',
                     'protocol_whitelist':'none','max_pixels':str(MAX_PIXELS)}) as clip:
            track=next(iter(clip.streams.video),None)
            if not track or not 0<track.width*track.height<=MAX_PIXELS or not track.time_base:
                return []
            track.thread_count=1
            start=track.start_time or 0
            duration=track.duration or 0
            targets=[start] if not duration else [start+duration*i//count for i in range(count)]
            samples=[]
            for target in targets:
                clip.seek(target,stream=track,backward=True,any_frame=False)
                selected=None
                for index,frame in enumerate(clip.decode(track)):
                    if not 0<frame.width*frame.height<=MAX_PIXELS:
                        return []
                    if frame.pts is not None:
                        selected=frame
                        if frame.pts>=target:
                            break
                    if index>=59:
                        break
                if selected is not None:
                    ms=round(float((selected.pts-start)*track.time_base)*1000)
                    if ms>=0 and (not samples or ms>samples[-1][0]):
                        samples.append((ms,base64.b64encode(png(selected)).decode('ascii')))
            return samples


def local_samples(path,count):
    env={k:v for k,v in os.environ.items() if k.upper() in {'PATH','SYSTEMROOT','WINDIR','TMP','TEMP'}}
    try:
        proc=subprocess.run([sys.executable,'-I',str(Path(__file__).resolve()),str(path),str(count)],
                            env=env,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=8,check=True,
                            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        return [(ms,base64.b64decode(encoded,validate=True)) for ms,encoded in json.loads(proc.stdout)]
    except (OSError,subprocess.SubprocessError,ValueError):
        return []


def add(worker,event,thread,videos,base_ids,*,provider=None,image_count=0):
    """Keep every original ahead of optional stills and one timestamp manifest."""
    import httpx
    budget=min(MAX_FRAMES,8-len(base_ids)-1)
    if not videos or budget<=0:
        return []
    cached=worker.db.execute('select ids from video_previews where event=? and thread=?',(event,thread)).fetchone()
    if cached:
        return json.loads(cached[0])
    try:
        if provider is None:
            response=worker.api.get(f'threads/{thread}',timeout=5)
            response.raise_for_status()
            provider=response.json().get('provider')
        if provider!='codex':
            return []
        response=worker.api.get('engines/codex',timeout=5)
        response.raise_for_status()
        if response.json().get('capabilities',{}).get('images') is not True:
            return []
    except (httpx.HTTPError,ValueError):
        return []
    previews=[]
    labels=['下列图像是本机按时间点提取的静帧，只代表各自时间点；不含连续动作或音轨。']
    for index,(name,path) in enumerate(videos[:budget]):
        slots=max(1,(budget-len(previews))//min(len(videos)-index,budget-index))
        for ms,data in local_samples(path,slots):
            number=image_count+len(previews)+1
            previews.append((f'video-{index+1}-{ms}ms.png',data,'image/png'))
            labels.append(f'第 {number} 张图像：视频 {index+1}（{name[:100]}），距开头 {ms/1000:.3f} 秒。')
    if not previews:
        return []
    uploads=[*previews,('video-frame-timestamps.txt','\n'.join(labels).encode('utf-8'),'text/plain')]
    ids=[]
    for i,upload in enumerate(uploads):
        position=-i-1
        prior=worker.db.execute('select attachment from prepared where event=? and position=?',(event,position)).fetchone()
        if prior:
            ids.append(prior[0])
            continue
        response=worker.api.post(f'threads/{thread}/attachments',files={'file':upload})
        if response.status_code in (413,415,422):
            # The backend may have less room left than the per-turn budget.
            return []
        response.raise_for_status()
        attachment=response.json()['id']
        with worker.db:
            worker.db.execute('insert into prepared values(?,?,?)',(event,position,attachment))
        ids.append(attachment)
    with worker.db:
        worker.db.execute('insert into video_previews values(?,?,?)',(event,thread,json.dumps(ids)))
    return ids


if __name__=='__main__':
    print(json.dumps(decode(Path(sys.argv[1]),int(sys.argv[2]))))
