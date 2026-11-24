import json
import os
from contextlib import closing
from pathlib import Path

import httpx
import pytest

from app.feishu import voice
from app.feishu.ingress import receive
from app.feishu.publisher import Publisher
from app.feishu.routing import journal
from app.feishu.worker import Worker


def event(key,text,*,room='oc_owner',sender='ou_owner',parent=None):
    msg={'message_id':'om_'+key,'chat_type':'p2p','chat_id':room,'message_type':'text',
         'content':json.dumps({'text':text}),'parent_id':parent}
    return {'header':{'app_id':'cli_dudu','event_id':key,'tenant_key':'t'},'event':{
        'sender':{'sender_type':'user','sender_id':{'open_id':sender}},'message':msg}}


def config():
    return {'roles':{},'steward':{'app_id':'cli_dudu','tenant_key':'t','open_id':'ou_owner'}}


@pytest.mark.asyncio
async def test_native_owner_voice_retry_after_restart(tmp_path,monkeypatch):
    cfg=config()
    item=event('new','读给我听：你好，这是合成的本机朗读验证。')
    key=('cli_dudu','new')
    trace=[]
    # Cross-platform runs exercise the sender with a real locally encoded tone.
    if os.name!='nt':
        import av
        def tone(text,path):
            path.parent.mkdir(exist_ok=True)
            with av.open(str(path),'w',format='ogg') as dst:
                out=dst.add_stream('libopus',rate=48000)
                out.layout='mono'
                frame=av.AudioFrame('s16','mono',960)
                frame.sample_rate=48000
                frame.planes[0].update(bytes(frame.planes[0].buffer_size))
                for pkt in [*out.encode(frame),*out.encode(None)]:
                    dst.mux(pkt)
            return voice.duration(path)
        monkeypatch.setattr(voice,'synthesize',tone)
    with closing(journal(tmp_path/'db')) as db:
        pub=Publisher(cfg,db,tmp_path)
        worker=Worker(cfg,pub)
        assert not receive(db,cfg['steward'],event('no','读给我听：内容',sender='unknown'))
        assert receive(db,cfg['steward'],item)
        await worker.process(db.execute('select * from inbox').fetchone())
        receipt=db.execute('select * from private_voice').fetchone()
        assert receipt and receipt['duration']>0
        filename=voice.local_file(pub,*key)
        assert filename.read_bytes().startswith(b'OggS') and b'OpusHead' in filename.read_bytes()[:100]
        import av
        with av.open(str(filename)) as media:
            decoded=next(media.decode(audio=0))
            assert decoded.samples>0
    with closing(journal(tmp_path/'db')) as db:
        pub=Publisher(cfg,db,tmp_path)
        worker=Worker(cfg,pub)
        pub.headers=lambda who:{'Authorization':who['app_id']}
        def remote(req):
            assert req.headers['Authorization']=='cli_dudu'
            trace.append(req.url.path)
            if req.url.path.endswith('/im/v1/files'):
                form=req.read()
                assert b'OggS' in form and b'OpusHead' in form and b'duration' in form and b'opus' in form
                return httpx.Response(200,json={'code':0,'data':{'file_key':'uploaded'}})
            assert req.url.path.endswith('/im/v1/messages/om_new/reply')
            payload=json.loads(req.content)
            assert payload['msg_type']=='audio' and json.loads(payload['content'])=={'file_key':'uploaded'}
            assert payload['uuid']==receipt['outgoing']
            if len(trace)==2:
                raise httpx.ConnectError('synthetic pre-delivery failure')
            return httpx.Response(200,json={'code':0,'data':{'message_id':'om_audio'}})
        pub.http=httpx.Client(transport=httpx.MockTransport(remote))
        assert not receive(db,cfg['steward'],item)
        worker.flush()
        assert db.execute('select state from outbox').fetchone()[0]=='pending'
        db.execute('update retry set after=0')
        worker.flush()
        worker.flush()
        assert db.execute('select remote from outbox').fetchone()[0]=='om_audio'
        assert len(trace)==3 and trace.count('/open-apis/im/v1/files')==1


@pytest.mark.asyncio
async def test_speak_only_ordinary_completed_reply_in_same_owner_chat(tmp_path,monkeypatch):
    cfg=config()
    heard=[]
    def local(text,path):
        heard.append(text)
        return 500
    monkeypatch.setattr(voice,'synthesize',local)
    with closing(journal(tmp_path/'j')) as db:
        worker=Worker(cfg,Publisher(cfg,db,tmp_path))
        source=event('old','ordinary')
        assert receive(db,cfg['steward'],source)
        row=db.execute('select * from inbox').fetchone()
        db.execute('insert into direct_turns values(?,?,2,3)',('cli_dudu','old'))
        worker.direct_final(row,'done','普通回答的合成示例。')
        db.execute("update outbox set state='sent',remote='om_answer'")
        cases=[event('other','读给我听',room='other',parent='om_answer'),
               event('right','读给我听',parent='om_answer'),
               event('long','读给我听 '+'a'*501),
               event('private','读给我听',parent='om_answer')]
        for item in cases:
            key=item['header']['event_id']
            if key=='private':
                db.execute("insert into owner_readwrite_requests values('cli_dudu','old','om_old','ou_owner','t')")
            receive(db,cfg['steward'],item)
            await worker.process(db.execute('select * from inbox where event=?',(key,)).fetchone())
        assert heard==['普通回答的合成示例。']
        assert db.execute('select count(*) from private_voice').fetchone()[0]==1
        assert not voice.is_command('请用例子解释读给我听是什么意思')
