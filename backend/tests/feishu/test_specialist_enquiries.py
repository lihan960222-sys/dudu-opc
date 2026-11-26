import json
from contextlib import closing
import httpx
import pytest

from app.db import session
from app.feishu.ingress import receive
from app.feishu.publisher import Publisher
from app.feishu.routing import enqueue, journal
from app.feishu.worker import Worker
from app.services import agent_store, feature_tasks
from tests.services.thread_harness import sessions, owner


@pytest.mark.parametrize("message", "咋样了 进度 状态 当前状态 在干嘛 你好 hi hello 如何了 执行状态 什么状态 现在什么情况 现在什么状态".split()
                         + ["  HELLO! ", "咋样了？", "派工 #12"])
def test_dispatch_shortcuts(message):
    from app.feishu.enquiries import is_dispatch_enquiry
    assert is_dispatch_enquiry(message)
    assert not is_dispatch_enquiry("#12 的进度如何？")
    assert not is_dispatch_enquiry("history")


@pytest.mark.asyncio
async def test_specialist_replies_only_to_bound_owner_message_with_scoped_evidence(tmp_path,sessions,owner,monkeypatch):
    monkeypatch.setattr(session,'AsyncSessionLocal',sessions)
    async with sessions() as db:
        welder = await agent_store.create_agent(db,owner_id=owner,name='Weld')
        renderer = await agent_store.create_agent(db,owner_id=owner,name='Render')
        async def card(agent,project,title):
            result=await feature_tasks.route(db,owner_id=owner,agent_id=agent.id,project=project,
                title=title,event_key=title,text='checked',intent='independent')
            await db.commit()
            return result['task_id']
        task=await card(welder,'P','unitcheck')
        await card(renderer,'P','OTHER_ROLE')
        await card(welder,'Q','OTHER_PROJECT')
    cfg={'owner_id':owner,'project':'P','steward':{'app_id':'lead','open_id':'ou_steward'},
         'roles':{'Weld':{'app_id':'w','agent_id':welder.id,'chat_id':'room','owner_open_id':'ou_weld'}}}
    identity={'app_id':'w','enquiry_role':'Weld','tenant_key':'t','open_id':'ou_weld'}
    body={'header':{'app_id':'w','tenant_key':'t','event_id':'1'},'event':{
         'sender':{'sender_type':'user','sender_id':{'open_id':'ou_steward'}},
         'message':{'message_type':'text','message_id':'om_ask','chat_type':'p2p','content':json.dumps({'text':f'#{task} 核对了吗？'})}}}
    with closing(journal(tmp_path/'j')) as store:
        pub=Publisher(cfg,store,tmp_path)
        worker=Worker(cfg,pub)
        assert not receive(store,identity,body)
        body['event']['sender']['sender_id']['open_id']='ou_weld'
        body['event']['message']['chat_type']='group'
        assert not receive(store,identity,body)
        body['event']['message']['chat_type']='p2p'
        assert receive(store,identity,body)
        assert not receive(store,identity,body)
        key=enqueue(store,task,'Weld',{'text':'verified units'},root=True)
        store.execute("update outbox set state='sent',remote='om_group' where id=?",(key,))
        req=[]
        pub.headers=lambda role:{'Authorization':role['app_id']}
        def send(request):
            req.append(request)
            assert request.headers['Authorization']=='w'
            assert request.url.path.endswith('/om_ask/reply')
            text=json.loads(json.loads(request.content)['content'])['text']
            assert 'verified units' in text and 'OTHER_' not in text
            return httpx.Response(200,json={'code':0,'data':{'message_id':'om_reply'}})
        pub.http=httpx.Client(transport=httpx.MockTransport(send))
        await worker.tick()
        assert len(req)==1 and store.execute('select count(*) from submissions').fetchone()[0]==0
        forged=enqueue(store,task,'Weld',{'text':'arbitrary private','enquiry_reply':True,'reply_to':'om_other'})
        with pytest.raises(ValueError):
            pub.deliver(store.execute('select * from outbox where id=?',(forged,)).fetchone())
        store.execute('delete from outbox where id=?',(forged,))
        identity.update(chat_id='room',bot_open_id='ou_bot')
        message=body['event']['message']
        message.update(chat_type='group',chat_id='wrong-room',root_id='om_group',message_id='om_discuss',
                       content=json.dumps({'text':'@Weld #9999 验收依据是什么？'}))
        body['header']['event_id']='discussion'
        message['mentions']=[{'id':{'open_id':'ou_bot'}}]
        assert not receive(store,identity,body)
        message['chat_id']='room'
        body['event']['sender']['sender_id']['open_id']='other'
        assert not receive(store,identity,body)
        body['event']['sender']['sender_id']['open_id']='ou_weld'
        message['mentions']=[]
        assert not receive(store,identity,body)
        message['parent_id']='om_group'
        assert receive(store,identity,body)
        assert not receive(store,identity,body)
        def group_send(request):
            assert request.url.path.endswith('/om_discuss/reply') and request.headers['Authorization']=='w'
            envelope=json.loads(request.content)
            assert envelope['reply_in_thread']
            text=json.loads(envelope['content'])['text']
            assert 'verified units' in text and 'OTHER_' not in text and '这里是本任务的话题' in text
            return httpx.Response(200,json={'code':0,'data':{'message_id':'om_discussion_reply'}})
        pub.http=httpx.Client(transport=httpx.MockTransport(group_send))
        await worker.tick()
        assert store.execute("select task from outbox where remote='om_discussion_reply'").fetchone()[0]==task
        assert store.execute('select count(*) from submissions').fetchone()[0]==0

@pytest.mark.asyncio
async def test_employee_can_show_own_dispatch_conversation_in_owner_dm(sessions, owner):
    from app.db.models import AgentJob
    from app.feishu.enquiries import employee_updates
    from app.team.service import change_member
    async with sessions() as db:
        lead = await agent_store.create_agent(db, owner_id=owner, name="Lead", make_lead=True)
        worker = await agent_store.create_agent(db, owner_id=owner, name="Jensen")
        await change_member(db, owner, worker.id)
        job = AgentJob(owner_id=owner, requester_agent_id=lead.id, requester_thread_id=10,
                       requester_turn_id=20, assignee_agent_id=worker.id, brief="核对项目日志",
                       brief_hash="example", status="completed", result="已经核对完毕")
        db.add(job)
        await db.commit()
        assert "已完成" in await employee_updates(db, owner, worker.id, lead.id, "执行状态")
        record = await employee_updates(db, owner, worker.id, lead.id, f"派工 #{job.id}")
        assert job.brief in record and job.result in record
        assert job.result not in await employee_updates(db, owner, lead.id, lead.id, f"派工 #{job.id}")
