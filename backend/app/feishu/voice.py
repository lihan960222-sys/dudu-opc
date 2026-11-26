"""Speak explicit private text using Windows' installed speech engine and Feishu native audio."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from .routing import enqueue

COMMAND=re.compile(r'^读给我听(?=$|[\s：:])')
LIMIT=500
MAX_SECONDS=180
# This is constant code. Neither the spoken text nor a path is interpolated into PowerShell.
SPEAK=r"""
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Speech
[Console]::InputEncoding=New-Object System.Text.UTF8Encoding($false)
$text=[Console]::In.ReadToEnd()
$engine=New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $lang=if($text -match '[\u3400-\u9fff]'){'zh'}else{'en'}
    $local=$engine.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Name -match '^Microsoft .+ Desktop$' }
    $voice=$local | Where-Object { $_.VoiceInfo.Culture.TwoLetterISOLanguageName -eq $lang } | Select-Object -First 1
    if (-not $voice) {throw 'No matching local Windows desktop voice'}
    $engine.SelectVoice($voice.VoiceInfo.Name)
    $engine.SetOutputToWaveFile([IO.Path]::Combine([Environment]::CurrentDirectory,'speech.wav'))
    $engine.Speak($text)
} finally {$engine.Dispose()}
"""


def is_command(text):
    return bool(COMMAND.match(text.strip()))


def duration(path):
    import av
    with av.open(str(path),format='ogg') as src:
        track=src.streams.audio[0]
        if track.codec_context.name!='opus' or not track.duration:
            raise ValueError('系统未能生成有效的 Opus 语音。')
        ms=round(track.duration*track.time_base*1000)
        if not 0<ms<=MAX_SECONDS*1000:
            raise ValueError('这段文字的朗读时间超过上限，请分段发送。')
        return ms


def synthesize(text,target):
    if os.name!='nt' or not 0<len(text)<=LIMIT:
        raise ValueError('朗读需要 Windows 本机语音，且每次最多 500 字。')
    import av
    from app.core.host_exec import ensure_allowed
    ensure_allowed('Windows offline text-to-speech')
    shell=Path(os.environ['SystemRoot'])/'System32'/'WindowsPowerShell'/'v1.0'/'powershell.exe'
    target.parent.mkdir(parents=True,exist_ok=True)
    if target.is_file():
        return duration(target)
    with tempfile.TemporaryDirectory(dir=target.parent) as cwd:
        env={k:v for k,v in os.environ.items() if k.upper() in
             ('SYSTEMROOT','WINDIR','SYSTEMDRIVE','USERPROFILE','APPDATA','LOCALAPPDATA','TEMP','TMP','PROGRAMDATA','PATH')}
        subprocess.run([str(shell),'-NoLogo','-NoProfile','-NonInteractive','-Command',SPEAK],
            cwd=cwd,env=env,input=text.encode('utf-8'),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            check=True,timeout=35,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        wav=Path(cwd)/'speech.wav'
        if wav.stat().st_size>32*1024*1024:
            raise ValueError('朗读音频超过上限，请分段发送。')
        encoded=Path(cwd)/'speech.opus'
        with av.open(str(wav),format='wav') as src,av.open(str(encoded),'w',format='ogg') as dst:
            out=dst.add_stream('libopus',rate=48000)
            out.layout='mono'
            out.bit_rate=32000
            samples=0
            for frame in src.decode(audio=0):
                samples+=frame.samples
                if samples>frame.sample_rate*MAX_SECONDS:
                    raise ValueError('这段文字的朗读时间超过上限，请分段发送。')
                for pkt in out.encode(frame):
                    dst.mux(pkt)
            for pkt in out.encode(None):
                dst.mux(pkt)
        ms=duration(encoded)
        os.replace(encoded,target)
        return ms


def local_file(publisher,app,event):
    key=hashlib.sha256(json.dumps([app,event],separators=(',',':')).encode()).hexdigest()
    root=publisher.directory.resolve()/'private-voice'
    path=root/(key+'.opus')
    if path.resolve()!=path:
        raise ValueError('朗读文件路径无效。')
    return path


async def handle(worker,row,msg,text):
    if msg.get('message_type')!='text' or not is_command(text):
        return False
    identity=worker.cfg['steward']
    db=worker.db
    keys=(row['app'],row['event'])
    proof=db.execute("""select i.state from owner_readwrite_requests r join inbox i using(app,event)
            where r.app=? and r.event=? and r.message=? and r.owner=? and r.tenant=?""",
            (*keys,row['message'],identity.get('open_id'),identity.get('tenant_key'))).fetchone()
    if row['app']!=identity['app_id'] or msg.get('chat_type')!='p2p' or not proof:
        with db:
            db.execute("update inbox set state='ignored' where app=? and event=? and state='received'",keys)
        return True
    if proof['state']!='received':
        return True
    from .documents import quoted_answer
    body=text.strip()[len('读给我听'):].lstrip(' ：:\r\n\t') or quoted_answer(db,row,msg)
    if not body or len(body)>LIMIT:
        worker.direct_final(row,'done','老板，我还没拿到能朗读的文字。您可以在「读给我听」后附上最多 500 字，也可以引用我在这段私聊里的普通文字回复。')
        return True
    try:
        ms=await asyncio.to_thread(synthesize,body,local_file(worker.pub,*keys))
    except (ImportError,IndexError,OSError,ValueError,subprocess.SubprocessError):
        worker.direct_final(row,'done','老板，这次我没能在本机生成语音。需要核对 Windows 语音包和音频组件。如果文字较长，也可以分段朗读。')
        return True
    with db:
        change=db.execute("update inbox set state='done' where app=? and event=? and state='received'",keys)
        if change.rowcount:
            outgoing=enqueue(db,None,'steward',{'kind':'audio','private_voice':True,'reply_to':row['message']})
            db.execute('insert into private_voice values(?,?,?,?)',(outgoing,*keys,ms))
            db.execute('update delivery_timing set outgoing=? where app=? and event=?',(outgoing,*keys))
    return True


def resource(publisher,row,body):
    who=publisher.config['steward']
    ref=publisher.db.execute("""select v.app,v.event,r.message,v.duration from private_voice v
        join owner_readwrite_requests r using(app,event)
        where v.outgoing=? and v.app=? and r.owner=? and r.tenant=?""",
        (row['id'],who['app_id'],who.get('open_id'),who.get('tenant_key'))).fetchone()
    if (not ref or row['role']!='steward' or row['task'] is not None or body.get('kind')!='audio'
            or body.get('reply_to')!=ref['message']):
        raise ValueError('朗读只能回复原来的老板私聊。')
    return local_file(publisher,ref['app'],ref['event']),ref['duration']
