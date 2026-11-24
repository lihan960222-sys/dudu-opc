import json
import os
from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func,select

from app.db.models import WorkItem
from app.feishu import setup_specialist, suggestions
from app.feishu.publisher import Publisher
from app.feishu.routing import journal
from app.feishu.worker import Worker
from tests.services.test_steward_suggestions import state,scan


@pytest.mark.asyncio
async def test_gateway_choice_creates_one_task_through_real_backend(state,tmp_path):
    sessions,client,_,source_id,lead,_=state
    await scan(sessions)
    cfg={'proactive_suggestions':True,'steward':{'app_id':'dudu'}}
    with closing(journal(tmp_path/'journal')) as db, TestClient(client._transport.app) as backend:
        w=Worker(cfg,Publisher(cfg,db,tmp_path))
        w.api=backend
        suggestions.poll(w)
        offer=db.execute('select * from outbox').fetchone()
        assert '1.' in json.loads(offer['body'])['text'] and '2.' in json.loads(offer['body'])['text']
        db.execute("update outbox set state='sent', remote='om_offer'")
        msg={'chat_type':'p2p','parent_id':'om_offer','message_type':'text','content':'{"text":"2"}'}
        db.execute("insert into inbox(app,event,message,payload) values('dudu','e','om_decision',?)",(json.dumps(msg),))
        row=db.execute('select * from inbox').fetchone()
        assert suggestions.answer(w,row,msg,'2')
        assert suggestions.answer(w,row,msg,'2')
        receipt=json.loads(db.execute('select body from outbox order by rowid desc limit 1').fetchone()[0])['text']
        async with sessions() as sql:
            assert await sql.scalar(select(func.count(WorkItem.id)))==2
            child=await sql.scalar(select(WorkItem).where(WorkItem.id!=source_id))
            assert child.agent_id==lead and f'#{child.id}' in receipt


@pytest.mark.skipif(os.name!='nt',reason='Windows current-user DPAPI')
def test_specialist_secret_entry_uses_real_account_encryption(tmp_path,monkeypatch):
    config=tmp_path/'feishu.json'
    config.write_text(json.dumps({'steward':{'agent_id':1,'app_id':'cli_a','secret_env':'FEISHU_STEWARD_SECRET'},'roles':{}}))
    secret='sample-only-temporary-dpapi-value'
    monkeypatch.setattr(setup_specialist,'prompt_secret',lambda:secret)
    monkeypatch.delenv('FEISHU_AGENT_9_SECRET',raising=False)
    setup_specialist.add(config,'绘图',9,'cli_x','ou_owner','oc_work')
    assert secret not in config.read_text('utf8')
    encrypted=setup_specialist.credential_path(config,'cli_x').read_bytes()
    assert secret.encode() not in encrypted
    setup_specialist.load_secrets(config,json.loads(config.read_text('utf8')))
    assert os.environ.pop('FEISHU_AGENT_9_SECRET')==secret
@pytest.mark.asyncio
async def test_official_sdk_event_and_real_media_bytes(tmp_path,monkeypatch):
    import io
    import av
    import httpx
    from docx import Document
    lark=pytest.importorskip('lark_oapi',reason='optional official Feishu SDK')
    from app.feishu.ingress import receive
    from app.feishu.routing import parts
    from app.services import attachments
    from tests.routers.test_attachments_api import png_bytes

    video=io.BytesIO()
    with av.open(video,'w','mp4') as container:
        stream=container.add_stream('mpeg4',rate=2)
        stream.width=stream.height=16
        stream.pix_fmt='yuv420p'
        for _ in range(2):
            frame=av.VideoFrame(16,16,'yuv420p')
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    sound=io.BytesIO()
    with av.open(sound,'w','ogg') as container:
        stream=container.add_stream('libopus',rate=48000)
        stream.layout='mono'
        frame=av.AudioFrame(format='s16',layout='mono',samples=960)
        frame.sample_rate=48000
        frame.planes[0].update(bytes(frame.planes[0].buffer_size))
        for packet in [*stream.encode(frame),*stream.encode()]:
            container.mux(packet)
    with av.open(io.BytesIO(sound.getvalue())) as clip:
        assert sum(f.samples for f in clip.decode(audio=0))>=960
    with av.open(io.BytesIO(video.getvalue())) as clip:
        assert len(list(clip.decode(video=0)))==2
    document=io.BytesIO()
    doc=Document()
    doc.add_paragraph('unit audit 17 MPa')
    doc.save(document)
    media={'image':('pixel.png',png_bytes(),'image/png'),'audio':('silence.ogg',sound.getvalue(),'audio/ogg'),
           'media':('clip.mp4',video.getvalue(),'video/mp4'),'file':('report.docx',document.getvalue(),'application/octet-stream')}
    identity={'app_id':'test','tenant_key':'t','open_id':'ou_owner'}
    monkeypatch.setattr(attachments,'root_dir',lambda:tmp_path/'thread-files')
    with closing(journal(tmp_path/'j')) as db:
        handler=lark.EventDispatcherHandler.builder('','').register_p2_im_message_receive_v1(
            lambda event: receive(db,identity,json.loads(lark.JSON.marshal(event)))).build()
        pub=Publisher({'steward':identity},db,tmp_path)
        pub.headers=lambda identity:{}
        pub.http=httpx.Client(transport=httpx.MockTransport(lambda req:
            httpx.Response(200,content=media[req.url.path.split('/')[-1]][1])))
        for kind,(name,data,mime) in media.items():
            message={'message_id':'om_'+kind,'message_type':kind,'chat_type':'p2p','content':
                json.dumps({'image_key' if kind=='image' else 'file_key':kind,'file_name':name})}
            event={'schema':'2.0','header':{'app_id':'test','tenant_key':'t','event_type':'im.message.receive_v1','event_id':kind},
                   'event':{'sender':{'sender_type':'user','sender_id':{'open_id':'ou_owner'}},'message':message}}
            handler._do_without_validation(json.dumps(event).encode())
            record=json.loads(db.execute('select payload from inbox where event=?',(kind,)).fetchone()[0])
            part=parts(kind,json.loads(record['content']))[0]
            path=pub.download(42,record['message_id'],part)
            assert path.read_bytes()==data
            if kind!='audio':
                stored=attachments.save(7,name,mime,data)
                assert stored.kind=={'image':'image','media':'video','file':'document'}[kind]
                if kind=='file':
                    assert '17 MPa' in stored.extracted_text
