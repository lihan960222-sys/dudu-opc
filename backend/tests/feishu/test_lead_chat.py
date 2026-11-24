"""Changing the steward model forks only its Feishu management conversation."""
import asyncio

import pytest
from sqlalchemy import select,func

from app.db import session
from app.db.models import AgentJob,FeishuLeadChat,Thread,Turn,WorkItem
from app.feishu.lead_chat import select_thread
from app.services import agent_store,feature_tasks,role_workspace,thread_store
from tests.services.thread_harness import sessions,owner


@pytest.mark.asyncio
async def test_switch_once_and_keep_old_conversation_and_tasks(tmp_path,sessions,owner,monkeypatch):
    monkeypatch.setattr(session,'AsyncSessionLocal',sessions)
    role=tmp_path/'Dudu'
    role.mkdir()
    role_workspace.initialize(role,'gpt-6.2-sol','Keep the project moving.')
    async with sessions() as db:
        lead=await agent_store.create_agent(db,owner_id=owner,name='Dudu',provider='codex',make_lead=True)
        staff=await agent_store.create_agent(db,owner_id=owner,name='Specialist',provider='codex',model='gpt-6.2-sol')
        lead.role_path=str(role)
        await db.commit()
        anchor=await thread_store.create_thread(db,owner_id=owner,agent_id=lead.id,provider='codex',model='gpt-6.2-sol',
            workspace_path=str(role),provider_session_id='native-private-original',provider_state={'thread_id':'native-old'})
        db.add(Turn(thread_id=anchor.id,seq=1,status='succeeded',prompt_text='PRIVATE_OLD_WORDS',model='gpt-6.2-sol'))
        await db.commit()
        task=await feature_tasks.route(db,owner_id=owner,agent_id=staff.id,project='P',title='Accepted delivery',
          event_key='established',text='DO_NOT_COPY_OLD_BRIEF',intent='independent')
        await db.commit()
    cfg={'owner_id':owner,'steward_thread':anchor.id,'project':'P','steward':{'agent_id':lead.id,'app_id':'app','open_id':'ou_owner','tenant_key':'tenant'}}
    assert await select_thread(cfg)==anchor.id
    (role/'AGENTS.md').write_text('---\ndefault_model: deepseek-flash\n---\nKeep the project moving.','utf-8')
    new,other=await asyncio.gather(select_thread(cfg),select_thread(cfg))
    assert new==other and new!=anchor.id
    async with sessions() as db:
        old=await db.get(Thread,anchor.id)
        current=await db.get(Thread,new)
        assert old.archived_at is None and old.provider_session_id=='native-private-original'
        assert current.agent_id==lead.id and current.model=='deepseek-flash' and current.workspace_path==old.workspace_path
        assert current.provider_session_id!=old.provider_session_id and not (current.provider_state or {}).get('thread_id')
        assert await db.scalar(select(func.count(Turn.id)).where(Turn.thread_id==new))==0
        assert await db.scalar(select(func.count(WorkItem.id)).where(WorkItem.owner_id==owner))==1
        assert (await db.get(WorkItem,task['task_id'])).agent_id==staff.id
        assert (await db.get(FeishuLeadChat,anchor.id)).isolated
        assert (await db.get(FeishuLeadChat,new)).isolated
    (role/'AGENTS.md').write_text('---\ndefault_model: gpt-6.2-sol\n---\nKeep the project moving.','utf-8')
    assert await select_thread(cfg)==anchor.id

def test_existing_steward_turn_survives_model_binding_migration(tmp_path):
    import sqlite3
    from tests.db.sqlite_helpers import run_alembic
    database=tmp_path/'existing.sqlite'
    old='st_project_consult_v1'
    for revision in (old,'head',old):
        result=run_alembic(database,'downgrade' if revision==old and database.exists() else 'upgrade',revision)
        assert result.returncode==0,result.stderr
        with sqlite3.connect(database) as db:
            if not db.execute('select 1 from thread').fetchone():
                thread=db.execute("insert into thread(owner_id,provider,model,provider_session_id) values('o','codex','gpt-6.2-sol','old-native')").lastrowid
                db.execute("insert into turn(thread_id,seq,prompt_text,status) values(?,1,'existing message','succeeded')",(thread,))
            assert db.execute('select thread.model,provider_session_id,prompt_text from thread join turn on turn.thread_id=thread.id').fetchall()==[
                ('gpt-6.2-sol','old-native','existing message')]
            present=db.execute("select 1 from sqlite_master where name='feishu_lead_chat'").fetchone()
            assert bool(present)==(revision=='head')
            if present:
                assert db.execute('select count(*) from feishu_lead_chat').fetchone()==(0,)


@pytest.mark.asyncio
async def test_assignment_receipt_cannot_forward_invented_approval(tmp_path, sessions, owner, monkeypatch):
    from contextlib import closing
    from types import SimpleNamespace
    import json
    from app.feishu.lead_chat import register_report
    from app.feishu.routing import journal
    monkeypatch.setattr(session, 'AsyncSessionLocal', sessions)
    async with sessions() as db:
        lead = await agent_store.create_agent(db, owner_id=owner, name='大嘴')
        staff = await agent_store.create_agent(db, owner_id=owner, name='Jensen')
        fields = dict(owner_id=owner, requester_agent_id=lead.id, requester_thread_id=7,
                      requester_turn_id=20, brief='列出两个目录名', brief_hash='demo', status='running')
        root = AgentJob(**fields, assignee_agent_id=lead.id, kind='root')
        db.add(root)
        await db.flush()
        db.add(AgentJob(**fields, assignee_agent_id=staff.id, kind='job', parent_id=root.id, root_id=root.id))
        await db.commit()
    with closing(journal(tmp_path/'channel')) as channel:
        worker = SimpleNamespace(cfg={'owner_id':owner,'steward':{'agent_id':lead.id}}, db=channel)
        row = dict(thread=7, turn=20, app='test', event='e', message='om_test')
        for _ in range(2):
            assert await register_report(worker, row, {'result':'你得去他的会话批准，否则会超时。'})
        bodies = [json.loads(r[0]) for r in channel.execute('select body from outbox')]
        assert bodies == [{'text':'已交给 Jensen，有结果我在这里回复。','reply_to':'om_test'}]


@pytest.mark.asyncio
async def test_escalated_hire_notifies_original_steward_message_once(tmp_path, sessions, owner, monkeypatch):
    from contextlib import closing
    import json
    from types import SimpleNamespace
    from app.core.config import settings
    from app.db.models import TeamSetupRequest
    from app.feishu.lead_chat import publish_reports
    from app.feishu.routing import journal
    monkeypatch.setattr(session, 'AsyncSessionLocal', sessions)
    monkeypatch.setattr(settings, 'SUPARAGENT_PUBLIC_URL', 'https://home.example.ts.net')
    async with sessions() as db:
        lead = await agent_store.create_agent(db, owner_id=owner, name='大嘴', make_lead=True)
        manager = await agent_store.create_agent(db, owner_id=owner, name='Jensen')
        fields = dict(owner_id=owner, requester_agent_id=lead.id, requester_thread_id=7,
                      requester_turn_id=20, brief='需要下属', brief_hash='demo', status='waiting')
        root = AgentJob(**fields, assignee_agent_id=lead.id, kind='root')
        db.add(root)
        await db.flush()
        job = AgentJob(**fields, assignee_agent_id=manager.id, kind='job', parent_id=root.id, root_id=root.id)
        db.add(job)
        await db.flush()
        plan = {'_origin_job': job.id, '_manager': manager.id, '_version': 1, 'summary': 'https://wrong.example'}
        hire = TeamSetupRequest(owner_id=owner, team_id=lead.team_id, lead_agent_id=lead.id, thread_id=7,
                                turn_id=20, request_id='hire-test', kind='setup', state='pending', plan=plan)
        db.add(hire)
        await db.commit()
    with closing(journal(tmp_path/'channel')) as channel:
        channel.execute('create table direct_turns(app text,event text,thread integer,turn integer)')
        channel.execute("insert into direct_turns values('app','e',7,20)")
        channel.execute("insert into inbox(app,event,message) values('app','e','om_original')")
        channel.execute("insert into direct_team_reports(app,event,job) values('app','e',?)", (root.id,))
        worker = SimpleNamespace(cfg={'owner_id': owner, 'steward': {'agent_id': lead.id, 'app_id': 'app'}}, db=channel)
        await publish_reports(worker)
        assert not channel.execute('select 1 from outbox').fetchall()
        async with sessions() as db:
            stored = await db.get(TeamSetupRequest, hire.id)
            stored.plan = {**plan, '_owner_review': True}
            await db.commit()
        await publish_reports(worker)
        await publish_reports(worker)
        bodies = [json.loads(row[0]) for row in channel.execute('select body from outbox')]
        assert len(bodies) == 1 and bodies[0]['reply_to'] == 'om_original'
        assert 'https://home.example.ts.net/chat?thread=7' in bodies[0]['text']
        assert 'wrong.example' not in bodies[0]['text']
