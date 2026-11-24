from types import SimpleNamespace as NS
import pytest
from sqlalchemy import select
from app.core.config import settings
from app.db.models import Agent,AgentJob,AgentJobInbox,Event,TeamSetupRequest
from app.services import agent_store,feature_tasks,thread_store
from app.team import service
from app.team.jobs import Jobs
from tests.services.thread_harness import sessions,owner
from tests.team.hiring_scenario import hire_in_dispatch


@pytest.mark.asyncio
@pytest.mark.parametrize("grant,decision,expected",[(True,"approve","applied"),(False,"approve","pending_owner"),(True,"reject","rejected")])
async def test_direct_employee_hire(sessions,owner,tmp_path,monkeypatch,grant,decision,expected):
    from app.services import local_claude_runner
    monkeypatch.setattr(settings,'STEWARD_STRICT_OWNER',owner)
    monkeypatch.setattr(local_claude_runner,'workspace_dir',lambda:tmp_path/'workspace')
    rt=NS(session=sessions,owner_id=owner,check_engine_model=lambda *args:None,hub=NS(notify=lambda _:None))
    rt.team_jobs=Jobs(rt)
    rt.team_jobs.kick=lambda:None
    async with sessions() as db:
        lead=await agent_store.create_agent(db,owner_id=owner,name='Lead',provider='codex',make_lead=True)
        lead.can_staff_team=grant
        manager=await agent_store.create_agent(db,owner_id=owner,name='Manager',provider='codex',model='gpt-test')
        await service.change_member(db,owner,manager.id)
        chat=await thread_store.create_thread(db,owner_id=owner,agent_id=lead.id,provider='codex',workspace_path=str(tmp_path))
        lead_turn=await thread_store.start_turn(db,chat.id,'build a team')
        card=(await feature_tasks.route(db,owner_id=owner,agent_id=manager.id,project='P',event_key='a',title='A',text='A'))['task_id']
        manager.role_path=str(tmp_path/'manager')
        root=tmp_path/'manager'/'任务'/str(card)
        root.mkdir(parents=True)
        thread,_=await feature_tasks.session_for(db,owner,card,model='gpt-test',workspace_path=str(root))
        job_id=await feature_tasks.dispatch(db,owner_id=owner,steward_id=lead.id,task_id=card,event_key='a',thread_id=thread.id)
        turn=await thread_store.start_turn(db,thread.id,'A',feature_event_key='a')
        job=await db.get(AgentJob,job_id)
        holder=await db.get(AgentJob,job.parent_id)
        holder.status='waiting'
        holder.assignee_thread_id=holder.requester_thread_id=job.requester_thread_id=chat.id
        job.requester_turn_id=lead_turn.id
        job.current_turn_id=turn.id
        job.reply='resume'
        job.status='running'
        await db.commit()
    result=await hire_in_dispatch(rt,owner,lead,manager,turn,lead_turn,decide=decision)
    assert result['state']==expected
    async with sessions() as db:
        child=await db.scalar(select(Agent).where(Agent.owner_id==owner,Agent.name=='Project reader'))
        job = await db.get(AgentJob,job_id)
        decisions = list((await db.scalars(select(AgentJobInbox).where(
            AgentJobInbox.holder_id == job_id, AgentJobInbox.kind.startswith('hire_decided:')))).all())
        assert await rt.team_jobs._outstanding(db,job)
        if expected == 'pending_owner':
            assert not decisions
            proposal = await db.scalar(select(TeamSetupRequest).where(TeamSetupRequest.request_id == result['request_id']))
            assert proposal.thread_id == chat.id and proposal.plan['_owner_review']
            cards = list(await db.scalars(select(Event).where(Event.type == 'team.setup.proposed',
                Event.data['request_id'].as_string() == result['request_id'])))
            assert [(card.thread_id,card.turn_id) for card in cards] == [(chat.id,lead_turn.id)]
        else:
            assert len(decisions) == 1
            message = await rt.team_jobs._resume_text(db,job,decisions)
            assert result['request_id'] in message and expected in message
            if child:
                assert f'"employee_id": {child.id}' in message
        if expected=='applied':
            assert child.manager_agent_id==manager.id and child.team_id==lead.team_id
            assert child.approval_mode=='plan' and not child.delegation_enabled and not child.can_staff_team
            assert child.role_path and child.role_path!=manager.role_path and child.model==manager.model
        else:
            assert not child
