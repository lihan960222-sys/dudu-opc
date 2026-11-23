import json
from contextlib import closing

import httpx
import pytest

from sqlalchemy import func, select
from app.db import session
from app.db.models import AgentJobInbox, Turn
from app.services import feature_tasks
from app.team import service
from app.feishu.ingress import receive
from app.feishu.publisher import Publisher
from app.feishu.routing import journal
from app.feishu.worker import Worker
from app.services import agent_store, role_workspace
from tests.services.thread_harness import sessions, owner


@pytest.mark.asyncio
async def test_task_followup_reuses_thread_after_bridge_restart(tmp_path,sessions,owner,monkeypatch):
    monkeypatch.setattr(session,'AsyncSessionLocal',sessions)
    specialist_dir = tmp_path / "specialist"
    specialist_dir.mkdir()
    role_workspace.initialize(specialist_dir,'test-model','Inspect the assigned task.')
    steward_dir = tmp_path / "steward"
    steward_dir.mkdir()
    role_workspace.initialize(steward_dir,'steward-model','Route messages to the right specialist.')
    from app.feishu import steward
    async def classify(model, instructions, roles, tasks, message, *, private_env):
        assert model == 'steward-model' and 'Route messages' in instructions
        assert message == '请让焊接组核对局部应变结果' and list(roles) == ['焊接']
        return 'deliver','焊接','局部应变',None
    monkeypatch.setattr(steward,'infer',classify)
    async with sessions() as db:
        agent = await agent_store.create_agent(db,owner_id=owner,name='Welder')
        lead = await agent_store.create_agent(db,owner_id=owner,name="Steward")
        lead.role_path = str(tmp_path / "steward")
        await service.set_lead(db,owner,lead.id)
        await service.change_member(db,owner,agent.id)
        agent.role_path = str(specialist_dir)
        await db.commit()
        agent_id = agent.id
    identity = {'app_id':'test','tenant_key':'tenant','open_id':'user','agent_id':lead.id}
    cfg = {'owner_id':owner,'project':'sample','isolated_runner_enabled':True,'steward':identity,
           'roles':{'焊接':{'agent_id':agent_id}}}
    calls = []
    turns = []
    original = feature_tasks.bind_turn
    async def bind_simulated_turn(db, owner_id, key, thread_id, turn_id):
        db.add(Turn(id=turn_id, thread_id=thread_id, seq=len(turns), prompt_text="test",
                    status="succeeded", result="ok"))
        await db.flush()
        await original(db, owner_id, key, thread_id, turn_id)
    monkeypatch.setattr(feature_tasks, "bind_turn", bind_simulated_turn)
    def backend(req):
        calls.append(req.url.path)
        if req.method == 'POST':
            turns.append(10000 + 100 * len(turns))
            return httpx.Response(202,json={'id':turns[-1]})
        report = json.dumps({'summary':'已核对','group_update':'已检查单位','artifacts':['产物/飞书/result.txt']})
        return httpx.Response(200,json={'turns':[{'id':turns[-1],'finished_at':'now','status':'succeeded','result':report}]})
    for n, text in enumerate(['请让焊接组核对局部应变结果','补充单位']):
        with closing(journal(tmp_path/'journal')) as store:
            worker = Worker(cfg,Publisher(cfg,store,tmp_path))
            worker.api = httpx.Client(base_url='http://localhost/api/v1/',transport=httpx.MockTransport(backend))
            event = {'header':{'app_id':'test','tenant_key':'tenant','event_id':str(n)},'event':{
                'sender':{'sender_type':'user','sender_id':{'open_id':'user'}},
                'message':{'message_id':f'm{n}','parent_id':'m0' if n else '', 'chat_type':'p2p',
                           'message_type':'text','content':json.dumps({'text':text})}}}
            assert receive(store,identity,event)
            row = store.execute("select * from inbox where state='received'").fetchone()
            await worker.process(row)
            running = store.execute("select * from inbox where state='running'").fetchone()
            assert running
            output = specialist_dir/'任务'/str(running['task'])/'产物'/'飞书'
            output.mkdir(parents=True,exist_ok=True)
            (output/'result.txt').write_text('validated')
            await worker.reconcile(running)
            publications = store.execute("select role,body from outbox").fetchall()
            assert {r['role'] for r in publications if 'snapshot' in json.loads(r['body'])} == {'steward','焊接'}
            async with sessions() as db:
                receipt = store.execute('select * from executions where turn=?',(turns[-1],)).fetchone()
                await feature_tasks.report(db, owner, receipt['event'], receipt['thread'], receipt['turn'])
                with pytest.raises(ValueError):
                    await feature_tasks.report(db, owner, receipt['event'], receipt['thread'], -1)
                await db.commit()
            assert store.execute('select count(*) from bindings').fetchone()[0] == 1
            assert store.execute('select count(distinct thread) from executions').fetchone()[0] == 1
    async with sessions() as db:
        assert await db.scalar(select(func.count(AgentJobInbox.id)).where(AgentJobInbox.owner_id == owner)) == 2
    assert calls[0]==calls[2]
