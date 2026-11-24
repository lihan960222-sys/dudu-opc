"""Installed Codex and native team MCP over loopback model fixtures; no paid credentials."""
import asyncio
import gzip
import json
import os
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import shutil
import sqlite3
import threading
import time
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db import session
from app.db.models import AgentJob,AgentJobInbox,Turn,Thread
from app.feishu.ingress import receive
from app.feishu.routing import journal
from app.feishu.worker import Worker
from app.feishu.publisher import Publisher
from app.feishu import direct_requests
from app.services import agent_store,codex_adapter,role_workspace,thread_store
from app.services.codex_adapter import CodexAdapter
from app.services.provider_adapter import EngineProbe,ModelOption
from app.services.thread_hub import ThreadHub
from app.services.thread_runtime import ThreadRuntime
from app.team import service
from tests.services.thread_harness import sessions,owner


class ModelFixture(BaseHTTPRequestHandler):
    def log_message(self,*_):
        pass

    def do_GET(self):
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        body=self.rfile.read(int(self.headers['content-length']))
        if self.headers.get('content-encoding')=='gzip':
            body=gzip.decompress(body)
        if self.headers.get('content-encoding')=='zstd':
            from compression import zstd
            body=zstd.decompress(body)
        req=json.loads(body)
        self.server.requests.append((self.path,req))
        model=req['model']
        call=uuid4().hex[:12]
        payload=[]
        if model=='deepseek-flash' and not self.server.delegated:
            toolname='mcp__team__delegate'
            namespace=None
            for tool in req.get('tools',[]):
                if tool.get('type')=='namespace' and 'team' in tool.get('name','') and any(
                        v.get('name')=='delegate' for v in tool.get('tools',[])):
                    toolname,namespace='delegate',tool['name']
            args={'agent_ids':[self.server.staff],'mode':'work','project':'Lab','task_title':'Fixture review',
                  'brief':'Review the synthetic release sample','done_when':'Report one check.','deliverables':['verification']}
            args=json.dumps(args)
            item={'type':'function_call','id':'fc_'+call,'call_id':'call_'+call,'name':toolname,'arguments':args,'status':'completed'}
            if namespace:
                item['namespace']=namespace
            payload=[{'type':'response.output_item.added','output_index':0,'item':dict(item,status='in_progress',arguments='')},
                     {'type':'response.function_call_arguments.delta','item_id':item['id'],'output_index':0,'delta':args}]
            self.server.delegated=True
        else:
            if model=='deepseek-flash':
                text='LEAD_FINAL_7' if 'EMPLOYEE_PROOF_7' in json.dumps(req['input']) else 'Assignment requested.'
            else:
                text='EMPLOYEE_PROOF_7: the synthetic release sample passed.'
            item={'id':'msg_'+call,'type':'message','status':'completed','role':'assistant',
                  'content':[{'type':'output_text','text':text,'annotations':[]}]}
            payload=[{'type':'response.output_item.added','output_index':0,'item':dict(item,status='in_progress',content=[])},
                     {'type':'response.output_text.delta','output_index':0,'item_id':item['id'],'content_index':0,'delta':text}]
        resp={'id':'resp_'+call,'object':'response','created_at':int(time.time()),'model':model,
              'status':'completed','output':[item],'usage':{'input_tokens':12,'output_tokens':4,'total_tokens':16}}
        events=[{'type':'response.created','response':dict(resp,status='in_progress',output=[])},*payload,
                {'type':'response.output_item.done','output_index':0,'item':item},
                {'type':'response.completed','response':resp}]
        data=''.join('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n' for e in events).encode()
        self.send_response(200)
        self.send_header('Content-Type','text/event-stream')
        self.send_header('Content-Length',str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.mark.skipif(os.name!='nt' or os.getenv('SUPARAGENT_TEST_NATIVE_CODEX_HANDOFF')!='1',
                    reason='Requires the installed Windows Codex and strict task filesystem runner')
@pytest.mark.asyncio
async def test_feishu_deepseek_lead_delegates_to_gpt_and_receives_report(tmp_path,sessions,owner,monkeypatch):
    from app.services import steward_deepseek
    from app.services import workspace_snapshots
    exe=shutil.which('codex.exe')
    if not exe:
        pytest.skip('Codex unavailable')
    server=ThreadingHTTPServer(('127.0.0.1',0),ModelFixture)
    server.requests=[]
    server.delegated=False
    listener=threading.Thread(target=server.serve_forever,daemon=True)
    listener.start()
    rt=ThreadRuntime(hub=ThreadHub(),adapters={'codex':CodexAdapter()},_session_factory=sessions,owner_id=owner)
    try:
        origin=f'http://127.0.0.1:{server.server_port}'
        home=tmp_path/'openai-home'
        home.mkdir()
        (home/'auth.json').write_text(json.dumps({'OPENAI_API_KEY':'sk-local-fixture'}))
        (home/'config.toml').write_text('openai_base_url="'+origin+'/gpt"\nanalytics.enabled=false\n','utf-8')
        monkeypatch.setenv('CODEX_HOME',str(home))
        monkeypatch.setenv('CODEX_SQLITE_HOME',str(home))
        monkeypatch.setenv('OPENAI_BASE_URL',origin+'/gpt')
        monkeypatch.setenv('OPENAI_API_KEY','sk-local-fixture')
        monkeypatch.setattr(codex_adapter,'DEEPSEEK_BASE_URL',origin+'/deep/')
        monkeypatch.setattr(codex_adapter,'deepseek_home',lambda:tmp_path/'deepseek-home')
        monkeypatch.setattr(steward_deepseek,'api_key',lambda:'sk-local-deepseek')
        monkeypatch.setattr(codex_adapter,'resolve_codex_executable',lambda:Path(exe))
        async def fixture_catalog(self,*,models=True):
            return EngineProbe(installed=True,ready=True,signed_in=True,models=[ModelOption('gpt-6.2-sol','GPT')])
        monkeypatch.setattr(CodexAdapter,'probe',fixture_catalog)
        monkeypatch.setattr(session,'AsyncSessionLocal',sessions)
        monkeypatch.setattr(settings,'TEAM_ENABLED',True)
        monkeypatch.setattr(settings,'TEAM_LEAD_AUTO',True)
        monkeypatch.setattr(settings,'STEWARD_STRICT_OWNER',owner)
        monkeypatch.setattr(workspace_snapshots,'store_dir_for',lambda workspace:tmp_path/'snapshots'/Path(workspace).name)
        lead_space=tmp_path/'manager'
        lead_space.mkdir()
        role=tmp_path/'analyst'
        role.mkdir()
        role_workspace.initialize(role,'gpt-6.2-sol','Inspect the assigned synthetic sample.')
        async with sessions() as db:
            lead=await agent_store.create_agent(db,owner_id=owner,name='Manager',provider='codex',model='deepseek-flash',make_lead=True)
            staff=await agent_store.create_agent(db,owner_id=owner,name='Reviewer',provider='codex',model='gpt-6.2-sol')
            await service.change_member(db,owner,staff.id)
            staff.role_path=str(role)
            await db.commit()
            anchor=await thread_store.create_thread(db,owner_id=owner,agent_id=lead.id,provider='codex',model='gpt-6.2-sol',
                                                      workspace_path=str(lead_space),provider_session_id='previous-native-session')
            db.add(Turn(thread_id=anchor.id,prompt_text='PRIVATE_OLD_CHAT_MUST_NOT_CROSS',status='succeeded',seq=1))
            await db.commit()
        server.staff=staff.id
        cfg={'owner_id':owner,'project':'Lab','steward_thread':anchor.id,
             'steward':{'agent_id':lead.id,'open_id':'ou_owner','tenant_key':'tenant','app_id':'app'}}
        cfg['roles']={}
        wire=[]
        def feishu_http(req):
            if req.method=='POST':
                wire.append((req.url.path,json.loads(json.loads(req.content)['content'])['text']))
            return httpx.Response(200,json={'code':0,'data':{'message_id':'om_'+str(len(wire))}})
        def gateway():
            store=journal(tmp_path/'channel.sqlite3')
            publisher=Publisher(cfg,store,tmp_path)
            publisher.headers=lambda role:{}
            publisher.http=httpx.Client(transport=httpx.MockTransport(feishu_http))
            return Worker(cfg,publisher)
        worker=gateway()
        text='Ask the reviewer to check the synthetic release sample.'
        event={'header':{'app_id':'app','tenant_key':'tenant','event_id':'event7'},
               'event':{'sender':{'sender_type':'user','sender_id':{'open_id':'ou_owner'}},
                        'message':{'message_id':'om_owner_request','chat_type':'p2p','message_type':'text',
                                   'content':json.dumps({'text':text})}}}
        assert receive(worker.db,cfg['steward'],event)
        row=worker.db.execute('select * from inbox').fetchone()
        def offline(request):
            raise httpx.ConnectError('fixture disconnect',request=request)
        worker.api=httpx.Client(transport=httpx.MockTransport(offline),base_url='http://local/')
        with pytest.raises(httpx.ConnectError):
            await worker.process(row)
        saved=worker.db.execute('select thread,body from direct_requests').fetchone()
        body=json.loads(saved['body'])
        thread=saved['thread']
        assert thread!=anchor.id and body['prompt_text']==text
        turn=await rt.send_turn(thread,body['prompt_text'],direct_event_key=body['direct_event_key'])
        turn_view={'id':turn.id}
        def backend(req):
            if req.method=='POST':
                assert json.loads(req.content)==body
                return httpx.Response(202,json={'id':turn.id})
            return httpx.Response(200,json={'turns':[turn_view]})
        def connect(worker):
            worker.api=httpx.Client(transport=httpx.MockTransport(backend),base_url='http://local/')
        connect(worker)
        direct_requests.send(worker,row)
        statuses=[]
        async with asyncio.timeout(120):
            while True:
                async with sessions() as db:
                    rows=(await db.scalars(select(Turn).where(Turn.thread_id==thread).order_by(Turn.seq))).all()
                    jobs=(await db.scalars(select(AgentJob).where(AgentJob.owner_id==owner))).all()
                    statuses=[(t.status,t.error_code,t.result) for t in rows]
                    if any(t.status=='failed' for t in rows) or any(j.status=='failed' for j in jobs):
                        pytest.fail(str((statuses,[(j.status,j.outcome_detail) for j in jobs],[(p,r['model']) for p,r in server.requests])))
                    initial=rows[0]
                    if initial.finished_at and 'finished_at' not in turn_view:
                        turn_view.update(status=initial.status,result=initial.result,finished_at=initial.finished_at.isoformat())
                        await worker.tick()
                        assert worker.db.execute('select job from direct_team_reports').fetchone()
                        worker.db.close()
                        worker=gateway()
                        connect(worker)
                    if any(j.kind=='root' and j.status=='completed' for j in jobs):
                        break
                await asyncio.sleep(.2)
        await worker.tick()
        await worker.tick()
        assert wire.count(('/open-apis/im/v1/messages/om_owner_request/reply','LEAD_FINAL_7'))==1,wire
        assert worker.db.execute('select finished from direct_team_reports').fetchone()[0]==1
        worker.db.close()
        assert turn.thread_id==thread
        assert all('PRIVATE_OLD_CHAT_MUST_NOT_CROSS' not in json.dumps(req) for _,req in server.requests)
        assert {req['model'] for _,req in server.requests if '/deep/' in _}=={'deepseek-flash'}
        assert {req['model'] for _,req in server.requests if '/gpt/' in _}=={'gpt-6.2-sol'}
        async with sessions() as db:
            child=await db.scalar(select(AgentJob).where(AgentJob.owner_id==owner,AgentJob.assignee_agent_id==staff.id))
            assert child.status=='completed' and child.task_id and 'EMPLOYEE_PROOF_7' in child.result
            specialist=await db.get(Thread,child.assignee_thread_id)
            assert specialist.provider_state.get("service_history")
            assert not list((home/"sessions").rglob("rollout-*.jsonl"))
            for index in home.glob("state_*.sqlite"):
                with sqlite3.connect(index.as_uri() + "?mode=ro", uri=True) as desktop:
                    assert desktop.execute("select count(*) from threads").fetchone() == (0,)
            assert specialist.agent_id==staff.id and specialist.model=='gpt-6.2-sol'
            inbox=await db.scalar(select(AgentJobInbox).where(AgentJobInbox.job_id==child.id))
            assert inbox and inbox.delivered_at
    finally:
        await rt.shutdown()
        server.shutdown()
        server.server_close()
        listener.join()
