import json
from contextlib import closing

import httpx
import pytest
from fastapi import FastAPI
import asyncio

from app.feishu.ingress import receive
from app.feishu.publisher import Publisher
from app.feishu.routing import journal
from app.feishu.worker import Worker
from app.feishu import video_frames
from app.services import agent_store,attachments
from app.services.local_access_policy import require_local_access
from app.services.provider_adapter import Capabilities,ProviderStatus,TurnOutcome
from tests.services.thread_harness import sessions,owner,rt,new_thread,finished


def mp4(path):
    import av
    with av.open(str(path),'w',format='mp4') as out:
        stream=out.add_stream('mpeg4',rate=2)
        stream.width=stream.height=16
        stream.pix_fmt='yuv420p'
        for n in range(6):
            frame=av.VideoFrame(16,16,'yuv420p')
            for plane in frame.planes:
                plane.update(bytes([32*n])*plane.buffer_size)
            for pkt in stream.encode(frame):
                out.mux(pkt)
        for pkt in stream.encode():
            out.mux(pkt)


class CaptureCodex:
    key='codex'
    label='Capture'
    capabilities=Capabilities(images=True)

    def __init__(self):
        self.seen=[]

    async def status(self):
        return ProviderStatus(ready=True,version='fixture')

    async def run_turn(self,ctx,sink,gate):
        self.seen.append(ctx)
        return TurnOutcome('succeeded',result='sampled')


@pytest.mark.asyncio
async def test_actual_backend_receives_bounded_video_stills_after_lost_reply(tmp_path,rt,sessions,owner,monkeypatch):
    from app.routers import threads,engines
    from app.routers import attachments as uploads
    from app.db.models import Turn
    from sqlalchemy import select,func
    clip=tmp_path/'clip.mp4'
    mp4(clip)
    async with sessions() as db:
        lead=await agent_store.create_agent(db,owner_id=owner,name='Steward',make_lead=True)
    thread=await new_thread(sessions,owner,agent_id=lead.id,provider='codex',workspace_path=str(tmp_path))
    from app.db import session
    from app.feishu.__main__ import verify_chat
    monkeypatch.setattr(session,'AsyncSessionLocal',sessions)
    await verify_chat({'owner_id':owner,'steward':{'agent_id':lead.id}},thread.id)
    with pytest.raises(ValueError):
        await verify_chat({'owner_id':owner,'steward':{'agent_id':lead.id+1}},thread.id)
    adapter=CaptureCodex()
    rt.adapters['codex']=adapter
    monkeypatch.setattr(attachments,'root_dir',lambda:tmp_path/'files')
    app=FastAPI()
    for router in (threads.router,engines.router,uploads.router):
        app.include_router(router)
    app.dependency_overrides[threads.get_runtime]=app.dependency_overrides[engines.get_runtime]=lambda:rt
    app.dependency_overrides[require_local_access]=lambda:None
    identity={'app_id':'app','tenant_key':'t','open_id':'ou_owner','agent_id':lead.id}
    cfg={'roles':{},'steward':identity,'steward_thread':thread.id}
    msg={'message_type':'media','message_id':'om_clip','chat_type':'p2p','content':json.dumps({'file_key':'f','file_name':'clip.mp4'})}
    event={'header':{'event_id':'e','app_id':'app','tenant_key':'t'},'event':{'sender':{'sender_type':'user','sender_id':{'open_id':'ou_owner'}},'message':msg}}
    dbpath=tmp_path/'j'
    loop=asyncio.get_running_loop()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test/') as client:
        # Gateway HTTP calls are synchronous. Run its journal in its own thread,
        # and schedule the ASGI backend on the loop that owns the database pool.
        class Backend:
            def get(self,url,**kwargs):
                return asyncio.run_coroutine_threadsafe(client.get(url,**kwargs),loop).result(timeout=20)
            def post(self,url,**kwargs):
                return asyncio.run_coroutine_threadsafe(client.post(url,**kwargs),loop).result(timeout=20)

        class LostResponse(Backend):
            def post(self,url,**kwargs):
                response=super().post(url,**kwargs)
                if url.endswith('/direct-turns'):
                    assert response.status_code==202,response.text
                    raise httpx.ReadTimeout('backend committed, client lost reply')
                return response

        def gateway():
            with closing(journal(dbpath)) as db:
                pub=Publisher(cfg,db,tmp_path)
                pub.headers=lambda _:{}
                pub.http=httpx.Client(transport=httpx.MockTransport(lambda req:
                    httpx.Response(200,content=clip.read_bytes()) if req.method=='GET' else httpx.Response(200,json={'code':0,'data':{'message_id':'om_wait'}})))
                worker=Worker(cfg,pub)
                worker.api=LostResponse()
                receive(db,identity,event)
                with pytest.raises(httpx.ReadTimeout):
                    asyncio.run(worker.process(db.execute('select * from inbox').fetchone()))
                assert db.execute('select state from inbox').fetchone()[0]=='direct_submitting'
            with closing(journal(dbpath)) as db:
                from app.feishu import direct_requests
                worker=Worker(cfg,Publisher(cfg,db,tmp_path))
                worker.api=Backend()
                direct_requests.send(worker,db.execute('select * from inbox').fetchone())
                assert db.execute('select state from inbox').fetchone()[0]=='direct_running'
        await asyncio.to_thread(gateway)
        await finished(sessions,thread.id)
        assert len(adapter.seen)==1
        ctx=adapter.seen[0]
        assert 1<=len(ctx.images)<=3
        assert all(image['name'].endswith('ms.png') for image in ctx.images)
        assert ctx.codex_prompt.user_text=='请查看附件并回复。'
        notes=ctx.codex_prompt.background['attached_files']
        assert 'clip.mp4' in notes and '距开头' in notes and '不含连续动作' in notes
        async with sessions() as sql:
            assert await sql.scalar(select(func.count(Turn.id)).where(Turn.thread_id==thread.id))==1


def test_preview_skips_invalid_video_nonvision_and_exhausted_slots(tmp_path):
    broken=tmp_path/'broken.mp4'
    broken.write_bytes(b'not video')
    assert video_frames.local_samples(broken,3)==[]
    class Nonvision:
        def get(self,*args,**kwargs):
            return httpx.Response(200,json={'capabilities':{'images':False}},request=httpx.Request('GET','http://test'))
    with closing(journal(tmp_path/'j')) as db:
        worker=Worker({},Publisher({},db,tmp_path))
        worker.api=Nonvision()
        for provider,ids in [('claude',[]),('codex',list(range(7))),('codex',[])]:
            assert video_frames.add(worker,'e',1,[('clip',broken)],ids,provider=provider)==[]
