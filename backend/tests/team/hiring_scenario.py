"""A real persisted proposal and decision shared by the hierarchy integration scenarios."""
from types import SimpleNamespace as NS
from sqlalchemy import select
from app.db.models import Agent,AgentJobInbox
from app.team import staff_hiring
from app.team.jobs import Jobs


async def hire_in_dispatch(rt, owner, lead, manager, manager_turn, lead_turn, *, decide='approve'):
    ctx=NS(approval_mode='ask',team=NS(origin='person'))
    worker=NS(agent_id=manager.id,thread_id=manager_turn.thread_id,turn_id=manager_turn.id,owner_token=manager_turn.owner_token,ctx=ctx)
    steward=NS(agent_id=lead.id,thread_id=lead_turn.thread_id,turn_id=lead_turn.id,owner_token=lead_turn.owner_token,ctx=ctx)
    spec={'name':'Project reader','title':'Reader','instructions':'Read the assigned input and report findings.','first_task':'Check the example.'}
    proposal=await staff_hiring.call(rt,worker,'propose_direct_report',spec)
    assert (await staff_hiring.call(rt,worker,'propose_direct_report',spec))['request_id']==proposal['request_id']
    async with rt.session() as db:
        assert await db.scalar(select(AgentJobInbox.id).where(AgentJobInbox.kind=='hire:'+proposal['request_id']))
        assert not await db.scalar(select(Agent.id).where(Agent.owner_id==owner,Agent.name==spec['name']))
    result=await staff_hiring.call(rt,steward,'review_direct_report',{k:proposal[k] for k in ('request_id','version')}|{'decision':decide})
    return result
