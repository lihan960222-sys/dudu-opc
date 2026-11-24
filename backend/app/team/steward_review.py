"""One-operation review by the owner-selected steward of a scoped employee."""
import hashlib
from pathlib import Path

from sqlalchemy import select

from app.core.config import settings
from app.db.models import Agent, AgentJob, Approval, Thread, Turn, WorkItem
from app.db.models.feature import FeatureSession
from app.db.transactions import begin_write
from app.services import thread_store
from app.services.feature_files import FeatureFiles
from app.team import service


async def preview(db, owner, job, approval):
    if (settings.STEWARD_STRICT_OWNER != owner or not approval or job.owner_id != owner
            or job.kind != 'job' or job.mode != 'work' or not job.task_id or job.reply != 'resume'
            or job.origin_kind != 'person' or job.status not in ('running','needs_input')
            or job.approval_mode == 'plan' or approval.status != 'pending'
            or approval.tool != 'feature_files.write_text' or approval.thread_id != job.assignee_thread_id
            or approval.turn_id != job.current_turn_id or approval.turn_id != job.assignee_turn_id):
        return None
    team = await service.team_for(db, owner, lock=True)
    worker = await db.get(Agent, job.assignee_agent_id, with_for_update=True)
    lead = await db.get(Agent, job.requester_agent_id, with_for_update=True)
    if (not team or not worker or not lead or lead.id != team.lead_agent_id or lead.id == worker.id
            or any(a.team_id != team.id or a.owner_id != owner or a.archived_at for a in (worker,lead))
            or not lead.delegation_enabled or worker.approval_mode != 'accept_edits' or not worker.role_path):
        return None
    thread = await db.get(Thread, approval.thread_id)
    parent = await db.get(Thread, job.requester_thread_id)
    original = await db.get(Turn, job.requester_turn_id)
    card = await db.get(WorkItem, job.task_id)
    binding = await db.get(FeatureSession, approval.thread_id)
    if (not parent or parent.owner_id != owner or parent.agent_id != lead.id or parent.kind != 'chat'
            or parent.archived_at or parent.room_id or parent.job_id or not original or original.person_id is not None
            or not thread or thread.kind != 'feature' or thread.archived_at or thread.owner_id != owner
            or thread.agent_id != worker.id or not card or card.owner_id != owner or card.agent_id != worker.id
            or card.archived or card.completed_at or card.thread_id != thread.id or not binding or binding.task_id != card.id):
        return None
    root = Path(worker.role_path) / '任务' / str(card.id)
    args = approval.input or {}
    if (Path(thread.workspace_path or '.') != root or set(args) != {'path','text'}
            or not isinstance(args['text'],str) or len(args['text'].encode()) > 4096):
        return None
    try:
        target = FeatureFiles(root).path('task',args['path'])
        if target == root or target.is_dir():
            return None
    except (OSError, TypeError, ValueError):
        return None
    return {'job_id':job.id, 'approval_id':approval.id, 'turn_id':approval.turn_id, 'task_id':card.id,
            'path':args['path'], 'text':args['text'], 'sha256':hashlib.sha256(args['text'].encode()).hexdigest()}


async def decide(rt, run, args):
    from app.team.jobs import event
    from app.db.models import AgentJobEvent
    from datetime import timedelta
    choice = args.get('decision')
    if (choice not in ('allow','deny','escalate') or type(args.get('job_id')) is not int
            or type(args.get('turn_id')) is not int or not isinstance(args.get('approval_id'),str)
            or not isinstance(args.get('reason'),str) or not 0 < len(args['reason'].strip()) <= 300):
        raise service.TeamError('invalid_input','Supply the reviewed operation, decision and brief reason.')
    async with rt.session() as db:
        await begin_write(db)
        job = await db.get(AgentJob,args['job_id'],with_for_update=True)
        caller = await db.get(Turn,run.turn_id)
        if (not job or job.requester_thread_id != run.thread_id or job.requester_agent_id != run.agent_id
                or not caller or caller.thread_id != run.thread_id or caller.owner_token != run.owner_token
                or caller.person_id is not None or caller.status != 'running' or run.ctx.approval_mode == 'plan'):
            raise service.TeamError('outside_scope','Not the private dispatching steward.')
        request = await db.get(Approval,args['approval_id'],with_for_update=True)
        details = await preview(db,rt.owner_id,job,request)
        if not details or any(args.get(k)!=details[k] for k in ('path','sha256','turn_id')):
            raise service.TeamError('review_changed','The operation is outside the delegation or is no longer pending.')
        target = rt._runs.get(details['turn_id'])
        if not target or not target.ctx.feature_workspace or target.ctx.approval_mode == 'plan' or target.stop_requested or target.ctx.aborted:
            raise service.TeamError('turn_gone','The employee is no longer waiting for that operation.')
        if await db.scalar(select(AgentJobEvent.id).where(AgentJobEvent.job_id==job.id,
                AgentJobEvent.kind=='review_escalated', AgentJobEvent.detail['approval_id'].as_string()==request.id)):
            raise service.TeamError('escalated','This decision was passed to the owner.')
        audit = {'approval_id':request.id,'lead_turn_id':run.turn_id,'decision':choice,'reason':args['reason']}
        event(db,job,'review_escalated' if choice=='escalate' else 'steward_review',audit)
        if choice=='escalate':
            await db.commit()
            return {'status':'pending_owner'}
        status = 'allowed' if choice=='allow' else 'denied'
        won = await thread_store.decide_approval(db,request.id,status,by={'by':'steward','agent_id':run.agent_id},
            expected_turn_id=target.turn_id,owner_token=target.owner_token,
            fresh_after=service.now()-timedelta(seconds=settings.THREAD_LEASE_SECONDS))
        if not won:
            raise service.TeamError('turn_gone','The pending operation has changed.')
        waiter = rt._waiters.get(request.id)
        if waiter is not None and not waiter.done():
            waiter.set_result(status)
        rt.hub.notify(job.assignee_thread_id)
        return {'status':status,'approval_id':request.id}

async def history(db, job):
    """Recent decisions on this employee's project; exclude file contents and private conversation text."""
    from app.db.models import AgentJobEvent, Event
    from app.db.models.feature import FeatureScope
    recent = await db.scalars(select(Approval).join(FeatureSession,FeatureSession.thread_id==Approval.thread_id)
        .join(FeatureScope,FeatureScope.task_id==FeatureSession.task_id).join(WorkItem,WorkItem.id==FeatureScope.task_id)
        .where(WorkItem.owner_id==job.owner_id,WorkItem.agent_id==job.assignee_agent_id,FeatureScope.project==job.project,
               Approval.tool=='feature_files.write_text',Approval.status.in_(('allowed','denied')))
        .order_by(Approval.decided_at.desc()).limit(5))
    result = []
    for approval in recent:
        trace = await db.scalar(select(Event.data).where(Event.turn_id==approval.turn_id,Event.type=='approval.resolved',
            Event.data['approval_id'].as_string()==approval.id).order_by(Event.id.desc()).limit(1))
        escalated = await db.scalar(select(AgentJobEvent.id).join(AgentJob,AgentJob.id==AgentJobEvent.job_id)
            .where(AgentJob.owner_id==job.owner_id,AgentJobEvent.kind=='review_escalated',
                   AgentJobEvent.detail['approval_id'].as_string()==approval.id).limit(1))
        result.append({'path':str((approval.input or {}).get('path',''))[:200], 'decision':approval.status,
            'source':(trace or {}).get('by','person') if trace is not None else 'unknown','escalated':bool(escalated)})
    return result
