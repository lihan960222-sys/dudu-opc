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
