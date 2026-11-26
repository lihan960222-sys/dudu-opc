import json
from contextlib import asynccontextmanager

import httpx
import pytest

from app.db import session
from app.feishu import submissions
from app.feishu.publisher import Publisher
from app.feishu.routing import journal
from app.feishu.worker import Worker
from app.services import feature_tasks
from tests.services.thread_harness import owner, sessions  # noqa: F401


class Noop:
    async def commit(self):
        pass


@asynccontextmanager
async def fake_session():
    yield Noop()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['before_post','after_remote_commit','after_response'])
async def test_restart_reuses_exact_request(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(session,'AsyncSessionLocal',fake_session)
    cfg = {'owner_id':'o'}
    store = journal(tmp_path/'state')
    worker = Worker(cfg,Publisher(cfg,store,tmp_path))
    store.execute("insert into inbox(app,event,message,payload,state,task) values('a','e','m','{}','bound',1)")
    row = store.execute('select * from inbox').fetchone()
    submissions.prepare(store,row,11,'specialist','task','prompt',['id'])
    remote_turns = {}
    seen = []
    def api(req):
        assert req.url.path.endswith('/11/feature-turns')
        body = json.loads(req.content)
        seen.append(body)
        turn = remote_turns.setdefault(body['feature_event_key'],81)
        if len(seen)==1 and failure=='after_remote_commit':
            raise httpx.ReadTimeout('response lost')
        return httpx.Response(200,json={'id':turn})
    worker.api = httpx.Client(base_url='http://localhost/',transport=httpx.MockTransport(api))
    bound = []
    async def bind(db,owner,key,thread,turn):
        if not bound and failure=='after_response':
            bound.append(turn)
            raise OSError('gateway stopped before saving response')
        bound.append(turn)
    monkeypatch.setattr(feature_tasks,'bind_turn',bind)
    if failure!='before_post':
        with pytest.raises((httpx.ReadTimeout,OSError)):
            await submissions.send(worker,store.execute('select * from inbox').fetchone())
    store.close()
    store = journal(tmp_path/'state')
    resumed = Worker(cfg,Publisher(cfg,store,tmp_path))
    resumed.api = worker.api
    await submissions.send(resumed,store.execute('select * from inbox').fetchone())
    assert len(remote_turns)==1 and len({json.dumps(p,sort_keys=True) for p in seen})==1
    assert tuple(store.execute('select thread,turn from executions').fetchone()) == (11,81)
    assert store.execute('select state from inbox').fetchone()[0]=='running'
    async def failed_reconcile(row):
        raise ValueError('unverified turn')
    monkeypatch.setattr(resumed,'reconcile',failed_reconcile)
    monkeypatch.setattr(resumed,'flush',lambda: None)
    await resumed.tick()
    assert store.execute('select state from inbox').fetchone()[0]=='running'
    store.close()


@pytest.mark.parametrize("already_accepted", [False, True])
async def test_withdrawn_replay_adopts_only_matching_preexisting_turn(tmp_path,sessions,owner,monkeypatch,already_accepted):
    from contextlib import closing
    from types import SimpleNamespace
    from app.services import agent_store, thread_store
    from app.team import service
    monkeypatch.setattr(session,"AsyncSessionLocal",sessions)
    key = submissions.event_key({"app":"a","event":"e"})
    async with sessions() as db:
        lead = await agent_store.create_agent(db,owner_id=owner,name="Lead",make_lead=True)
        old = await agent_store.create_agent(db,owner_id=owner,name="First")
        new = await agent_store.create_agent(db,owner_id=owner,name="Next")
        for employee in (old,new):
            await service.change_member(db,owner,employee.id)
        initial = await feature_tasks.route(db,owner_id=owner,agent_id=old.id,event_key=key,
            project="P",title="Import",text="Certified request")
        task = initial["task_id"]
        thread, _ = await feature_tasks.session_for(db,owner,task,model="m",workspace_path=str(tmp_path))
        await feature_tasks.dispatch(db,owner_id=owner,steward_id=lead.id,task_id=task,event_key=key,thread_id=thread.id)
        if already_accepted:
            accepted = await thread_store.start_turn(db,thread.id,"Certified request",feature_event_key=key)
        await feature_tasks.reassign(db,task,1,new.id,"correction",owner_id=owner)
        await db.commit()
    with closing(journal(tmp_path/"recovery")) as channel, httpx.Client(base_url="http://localhost",
            transport=httpx.MockTransport(lambda req:httpx.Response(409,json={"detail":{"code":"dispatch_conflict"}}))) as api:
        worker = Worker({"owner_id":owner},SimpleNamespace(db=channel))
        worker.api.close()
        worker.api = api
        channel.execute("insert into inbox(app,event,message,payload,state,task) values('a','e','m','{}','bound',?)",(task,))
        row = channel.execute("select * from inbox").fetchone()
        submissions.prepare(channel,row,thread.id,"First","Import","Certified request",[])
        if already_accepted:
            request = channel.execute("select * from submissions").fetchone()
            assert not await submissions.adopt_withdrawn(worker,row,request,{"prompt_text":"changed","attachment_ids":[]})
        await submissions.send(worker,row)
        assert channel.execute("select state from inbox").fetchone()[0] == ("running" if already_accepted else "superseded")
        found = channel.execute("select turn from executions").fetchone()
        assert (found[0] if found else None) == (accepted.id if already_accepted else None)


@pytest.mark.asyncio
async def test_query_clarification_and_cross_specialist_new(tmp_path,monkeypatch):
    monkeypatch.setattr(session,'AsyncSessionLocal',fake_session)
    db = journal(tmp_path/'state')
    cfg = {'owner_id':'o','project':'p','steward':{'app_id':'app'},'roles':{'A':{'agent_id':1},'B':{'agent_id':2}}}
    worker = Worker(cfg,Publisher(cfg,db,tmp_path))
    db.execute("insert into bindings values(5,'A','热压')")
    def incoming(e,m,text,parent=None):
        payload = json.dumps({'content':json.dumps({'text':text}),'chat_type':'p2p','message_type':'text','parent_id':parent})
        db.execute('insert into inbox(app,event,message,payload) values(?,?,?,?)',('app',e,m,payload))
        return db.execute('select * from inbox where event=?',(e,)).fetchone()
    asked = incoming('first','om1','查询 A 热压')
    worker.respond(asked,'clarify','请选择')
    db.execute("update outbox set state='sent',remote='omQ'")
    captured=[]
    async def route(session,**kwargs):
        captured.append(kwargs)
        return {'action':'query','task_id':5,'status':'done'} if kwargs['intent']=='query' else {'action':'created','task_id':6}
    monkeypatch.setattr(feature_tasks,'route',route)
    await worker.process(incoming('choice','om2','#5','omQ'))
    assert (captured[-1]['intent'],captured[-1]['reference_id'])==('query',5)
    assert db.execute('select count(*) from submissions').fetchone()[0]==0
    await worker.process(incoming('next','om3','新建 B 渲染','om2'))
    assert captured[-1]['intent']=='independent' and captured[-1]['reference_id'] is None
