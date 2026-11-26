"""One-operation review by the owner-selected steward of a scoped employee."""
import hashlib
from pathlib import Path

from sqlalchemy import select

from app.core.config import settings
from app.db.models import Agent, AgentJob, Approval, DirectTurnReceipt, Event, FeishuLeadChat, Person, Thread, Turn, WorkItem
from app.db.models.feature import FeatureSession
from app.db.transactions import begin_write
from app.services import thread_store
from app.services.feature_files import FeatureFiles
from app.team import service


async def _owner_channel_request(db, owner, thread, turn):
    """A device's anonymous chat is not a gateway-authenticated owner DM."""
    from contextlib import closing
    import json
    import sqlite3

    from app.feishu.lead_chat import signature
    from app.feishu.submissions import event_key
    from app.services.feature_tasks import direct_input_hash
    from app.team.feishu_assignment import configuration

    receipt = await db.scalar(select(DirectTurnReceipt).where(DirectTurnReceipt.owner_id==owner,
        DirectTurnReceipt.turn_id==turn.id, DirectTurnReceipt.thread_id==thread.id))
    binding = await db.get(FeishuLeadChat, thread.id) if receipt else None
    if not binding:
        return False
    try:
        cfg = configuration()
        if not cfg or cfg["owner_id"]!=owner or cfg["steward"]["agent_id"]!=thread.agent_id:
            return False
        who = cfg["steward"]
        if binding.channel != signature(owner,who["app_id"],who["tenant_key"],who["open_id"],
                                        cfg.get("project",""),cfg["steward_thread"]):
            return False
        path = Path(cfg["_access_config_path"]).parent / cfg["state_dir"] / "channel.sqlite3"
        with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro", uri=True, timeout=1)) as journal:
            journal.row_factory = sqlite3.Row
            rows = journal.execute("""select r.app,r.event,r.body from direct_requests r
                join owner_dm_sources s using(app,event)
                where r.app=? and r.thread=? and s.owner=? and s.tenant=?
                  and json_extract(r.body,'$.direct_event_key')=?""",
                (who["app_id"],thread.id,who["open_id"],who["tenant_key"],receipt.event_key)).fetchall()
        if len(rows)!=1 or event_key(rows[0])!=receipt.event_key:
            return False
        body = json.loads(rows[0]["body"])
        source = {key:body[key] for key in ("owner_text","reply_to_turn_id","reference_event_key","reference_task_id")
                  if body.get(key) is not None}
        return receipt.input_sha256 == direct_input_hash(body["prompt_text"], body.get("attachment_ids"), body.get("skills"), source)
    except (OSError, KeyError, ValueError, TypeError, sqlite3.Error):
        return False


async def direct_project_mode(db, owner, worker, source_turn_id, requester_mode):
    """Use the employee's saved project grant only for a directly verified owner dispatch."""
    from app.services import agent_store, employee_runtime
    from app.services.routine_service import effective_approval_mode

    own = agent_store.employee_mode(owner, worker.approval_mode)
    common = effective_approval_mode(own, requester_mode)
    if (common == own or requester_mode != "ask" or own != "accept_edits" or owner!=settings.STEWARD_STRICT_OWNER
            or worker.owner_id!=owner or not worker.role_path or not employee_runtime.native_enabled(owner, worker.id)):
        return common
    turn = await db.get(Turn, source_turn_id)
    if not turn or turn.origin!="person":
        return common
    thread = await db.get(Thread, turn.thread_id)
    team = await service.team_for(db,owner)
    if (not team or worker.archived_at or worker.team_id!=team.id
            or worker.manager_agent_id not in (None,team.lead_agent_id)
            or not thread or thread.owner_id!=owner or thread.agent_id!=team.lead_agent_id
            or thread.kind!="chat" or thread.job_id or thread.room_id or thread.archived_at):
        return common
    started = await db.scalar(select(Event.data).where(Event.turn_id==turn.id, Event.type=="turn.started"))
    if not started or started.get("use_employee_role") is not True or started.get("internal") or started.get("team_generated"):
        return common
    if turn.person_id is not None:
        speaker = await db.get(Person, turn.person_id)
        verified = bool(speaker and speaker.role=="owner" and speaker.status=="active")
    else:
        verified = await _owner_channel_request(db, owner, thread, turn)
    return own if verified else common


async def delegated_mode(db, parent, caller, child, requester_mode):
    """A supervisor can pass on project writes only within the owner's standing grant."""
    from app.services.feature_tasks import assignment_current
    from app.services.routine_service import effective_approval_mode
    if (not parent or parent.kind != "job" or parent.mode != "work" or parent.origin_kind != "person"
            or parent.status not in ("running", "waiting", "needs_input", "resume")
            or parent.assignee_agent_id != caller.id or parent.owner_id != caller.owner_id
            or child.owner_id != caller.owner_id or child.manager_agent_id != caller.id
            or not caller.team_id or child.team_id != caller.team_id or child.archived_at or caller.archived_at
            or not caller.delegation_enabled or not caller.delegate_project_writes
            or not await assignment_current(db, parent)):
        return "plan"
    limit = effective_approval_mode(caller.approval_mode, child.approval_mode)
    return effective_approval_mode(limit, effective_approval_mode(parent.approval_mode, requester_mode))


async def effective_job_mode(db, job):
    from app.services.feature_tasks import assignment_current
    from app.services.routine_service import effective_approval_mode
    if job.mode != "work" or not await assignment_current(db, job):
        return "plan"
    team = await service.team_for(db, job.owner_id)
    if not team or not team.lead_agent_id:
        return "plan"
    mode = job.approval_mode or "ask"
    seen = set()
    while job and job.id not in seen:
        seen.add(job.id)
        worker = await db.get(Agent, job.assignee_agent_id)
        caller = await db.get(Agent, job.requester_agent_id)
        if (not worker or not caller or worker.archived_at or caller.archived_at or
                caller.owner_id != team.owner_id or worker.owner_id != team.owner_id or
                worker.team_id != team.id or caller.team_id != team.id or
                (worker.manager_agent_id or team.lead_agent_id) != caller.id or not caller.delegation_enabled):
            return "plan"
        mode = effective_approval_mode(mode, worker.approval_mode)
        if caller.id == team.lead_agent_id:
            return mode
        parent = await db.get(AgentJob, job.parent_id) if job.parent_id else None
        if not parent or parent.project != job.project or parent.assignee_thread_id != job.requester_thread_id:
            return "plan"
        mode = await delegated_mode(db, parent, caller, worker, mode)
        job = parent
    return "plan"


async def current_native_job(db, run, *, write=False, lock=False):
    """Recheck the bound dispatch, owner, turn and live write ceiling before a broker calls a tool."""
    from app.services.feature_tasks import assignment_current
    project = getattr(run.ctx, "project_runtime", None)
    if not project or run.ctx.aborted:
        return None
    query = select(AgentJob).where(
        AgentJob.owner_id == project.owner_id, AgentJob.assignee_agent_id == project.employee_id,
        AgentJob.assignee_thread_id == run.thread_id, AgentJob.assignee_turn_id == run.turn_id,
        AgentJob.task_id == project.task_id, AgentJob.assignment_epoch == project.assignment_epoch,
        AgentJob.kind == "job", AgentJob.mode == "work", AgentJob.status.in_(("running", "dispatched", "needs_input")))
    job = await db.scalar(query.with_for_update() if lock else query)
    if not job or not await assignment_current(db, job, lock=lock):
        return None
    if write and (not project.writable or run.ctx.approval_mode == "plan" or await effective_job_mode(db, job) == "plan"):
        return None
    return job


async def protect_active_scope(db, agent, *, shared_folders=False):
    """A live native process retains its startup file permissions until it exits."""
    rows = (await db.execute(select(Thread, Agent).join(Turn, Turn.thread_id == Thread.id)
        .join(Agent, Agent.id == Thread.agent_id).where(
            Thread.owner_id == agent.owner_id, Thread.kind == "feature", Turn.status == "running")
        .with_for_update())).all()
    for thread, worker in rows:
        if not (thread.provider_state or {}).get("employee_project"):
            continue
        shares = shared_folders and worker.team_workspaces_readable and worker.team_id == agent.team_id
        current, visited = worker, set()
        while current and current.id not in visited:
            visited.add(current.id)
            if shares or current.id == agent.id:
                raise service.TeamError("project_running", f"请先停止 {worker.name} 的当前项目回合，再更改这项权限。")
            if not current.manager_agent_id:
                team = await service.team_for(db, worker.owner_id)
                if team and team.lead_agent_id == agent.id and team.id == worker.team_id:
                    raise service.TeamError("project_running", f"请先停止 {worker.name} 的当前项目回合，再更改团队权限。")
                break
            current = await db.get(Agent, current.manager_agent_id)


async def preview(db, owner, job, approval):
    from app.services.feature_tasks import assignment_current
    if (settings.STEWARD_STRICT_OWNER != owner or not approval or job.owner_id != owner
            or job.kind != 'job' or job.mode != 'work' or not job.task_id or job.reply != 'resume'
            or job.origin_kind != 'person' or job.status not in ('running','needs_input')
            or job.approval_mode == 'plan' or approval.status != 'pending'
            or approval.tool != 'feature_files.write_text' or approval.thread_id != job.assignee_thread_id
            or approval.turn_id != job.current_turn_id or approval.turn_id != job.assignee_turn_id
            or not await assignment_current(db, job, lock=True)):
        return None
    team = await service.team_for(db, owner, lock=True)
    worker = await db.get(Agent, job.assignee_agent_id, with_for_update=True)
    lead = await db.get(Agent, job.requester_agent_id, with_for_update=True)
    if (not team or not worker or not lead or lead.id != team.lead_agent_id or lead.id == worker.id
            or any(a.team_id != team.id or a.owner_id != owner or a.archived_at for a in (worker,lead))
            or worker.manager_agent_id not in (None, lead.id)
            or not lead.delegation_enabled or worker.approval_mode != 'accept_edits' or not worker.role_path):
        return None
    thread = await db.get(Thread, approval.thread_id)
    parent = await db.get(Thread, job.requester_thread_id)
    original = await db.get(Turn, job.requester_turn_id)
    card = await db.get(WorkItem, job.task_id)
    binding = await db.get(FeatureSession, approval.thread_id)
    if (not parent or parent.owner_id != owner or parent.agent_id != lead.id or parent.kind != 'chat'
            or parent.archived_at or parent.room_id or parent.job_id or not original or original.person_id is not None
            or original.thread_id != parent.id
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
            'assignment_epoch':job.assignment_epoch, 'employee_id':worker.id,
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
        audit = {'approval_id':request.id,'lead_turn_id':run.turn_id,'decision':choice,'reason':args['reason'],
                 'assignment_epoch':job.assignment_epoch, 'employee_id':job.assignee_agent_id}
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
               FeatureSession.assignment_epoch==FeatureScope.assignment_epoch,
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
