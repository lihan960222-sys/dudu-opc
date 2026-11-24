import json
from contextlib import closing

import pytest
from app.db import session
from app.db.models import AgentJob
from app.feishu.publisher import Publisher
from app.feishu.routing import journal
from app.feishu.team_progress import refresh
from app.feishu.worker import Worker
from app.services import agent_store, feature_tasks
from tests.services.thread_harness import owner, sessions


@pytest.mark.asyncio
async def test_group_tracks_real_new_assignments_only(tmp_path,owner,sessions,monkeypatch):
    monkeypatch.setattr(session,'AsyncSessionLocal',sessions)
    async with sessions() as main:
        agent=await agent_store.create_agent(main,owner_id=owner,name='Role')
        result=await feature_tasks.route(main,owner_id=owner,project='P',title='Welding',text='scope',
                                         event_key='a',intent='independent',agent_id=agent.id)
        task_id=result['task_id']
        async def job(mode='work',project='P'):
            row=AgentJob(owner_id=owner,task_id=task_id,project=project,requester_agent_id=agent.id,
                 requester_thread_id=1,requester_turn_id=1,assignee_agent_id=agent.id,mode=mode,status='queued',
                 brief='PRIVATE_QUESTION',brief_hash='x')
            main.add(row)
            await main.commit()
            return row
        await job()
        cfg={'owner_id':owner,'project':'P','steward':{'app_id':'lead'},
             'roles':{'W':{'agent_id':agent.id,'chat_id':'work'}}}
        with closing(journal(tmp_path/'j')) as db:
            w=Worker(cfg,Publisher(cfg,db,tmp_path))
            await refresh(w)
            assert db.execute('select count(*) from outbox').fetchone()[0]==0
            assigned=await job()
            await job('consult')
            await job(project='Q')
            await refresh(w)
            assigned.status='completed'
            assigned.result='PRIVATE_RESULT'
            await main.commit()
            await refresh(w)
            await refresh(w)
            rows=db.execute('select * from outbox').fetchall()
            assert len(rows)==3 and sum(row['is_root'] for row in rows)==1
            assert {row['role'] for row in rows}=={'W'}
            messages=' '.join(json.loads(row['body'])['text'] for row in rows)
            assert '派工' in messages and '已完成' in messages and 'PRIVATE' not in messages
