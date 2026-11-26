"""Private hiring requests from a directly assigned specialist to its steward."""
from sqlalchemy import select

from app.core.config import settings
from app.db.models import Agent, AgentJob, AgentJobInbox, Thread, Turn, TeamSetupRequest
from app.db.transactions import begin_write
from app.services import thread_store
from app.team import service, setup


async def summary(db, owner, key, thread_id):
    row = await setup.existing(db,owner,key)
    if not row or row.thread_id != thread_id or row.state != 'pending' or '_manager' not in row.plan:
        return None
    return {'request_id':key,'version':setup.request_version(row),'manager_agent_id':row.plan['_manager'],
            'mode':'plan','plan':setup.public_plan(row.plan)}


async def pending_for_job(db, job):
    return await db.scalar(select(TeamSetupRequest.id).where(
        TeamSetupRequest.owner_id == job.owner_id, TeamSetupRequest.kind == 'setup',
        TeamSetupRequest.state == 'pending', TeamSetupRequest.plan['_origin_job'].as_integer() == job.id,
        TeamSetupRequest.plan['_manager'].as_integer() == job.assignee_agent_id).limit(1))


async def notify_requesting_job(db, row):
    job = await db.get(AgentJob, row.plan['_origin_job'], with_for_update=True)
    if (job is None or job.owner_id != row.owner_id or job.assignee_agent_id != row.plan['_manager']
            or job.requester_agent_id != row.lead_agent_id or job.requester_thread_id != row.thread_id
            or job.status not in ('running', 'waiting', 'resume', 'needs_input')):
        return
    db.add(AgentJobInbox(owner_id=job.owner_id, holder_id=job.id, job_id=job.id,
                        kind='hire_decided:' + row.request_id))


async def decision_for_job(db, job, key):
    row = await setup.existing(db, job.owner_id, key)
    if (not row or row.state not in ('applied', 'rejected') or row.plan.get('_origin_job') != job.id
            or row.plan.get('_manager') != job.assignee_agent_id):
        return None
    result = {'request_id': key, 'state': row.state, 'manager_agent_id': job.assignee_agent_id}
    if row.state == 'applied':
        result['employee_id'] = row.result['results'][0]['agent_id']
        result['first_task'] = setup.public_plan(row.plan)['operations'][0].get('first_task')
    return result


async def checked_manager(db, owner, team, lead, plan):
    manager = await db.get(Agent,plan['_manager'],with_for_update=True)
    if (not manager or manager.team_id != team.id or manager.owner_id != owner or manager.archived_at
            or manager.id == lead.id or manager.manager_agent_id not in (None,lead.id)
            or not manager.delegation_enabled):
        raise service.TeamError('manager_changed','The proposed supervisor is no longer a direct employee of this steward.')
    return manager


async def call(rt, run, name, args):
    owner = rt.owner_id
    if settings.STEWARD_STRICT_OWNER != owner or run.ctx.approval_mode == 'plan' or run.ctx.team.origin != 'person':
        raise service.TeamError('outside_staff_scope','Hiring requires a private steward assignment.')
    async with rt.session() as db:
        await begin_write(db)
        team = await service.team_for(db,owner,lock=True)
        actor = await service.member(db,owner,run.agent_id,team_id=team.id) if team else None
        turn = await db.get(Turn,run.turn_id,with_for_update=True)
        thread = await db.get(Thread,run.thread_id)
        if (not actor or not actor.delegation_enabled or not turn or turn.status!='running' or turn.owner_token!=run.owner_token
                or not thread or thread.agent_id!=actor.id or thread.owner_id!=owner or turn.thread_id!=thread.id or thread.archived_at):
            raise service.TeamError('outside_staff_scope','The originating assignment is no longer active.')
        lead = await service.member(db,owner,team.lead_agent_id,team_id=team.id)
        if name=='propose_direct_report':
            job=await db.scalar(select(AgentJob).where(AgentJob.owner_id==owner, AgentJob.assignee_agent_id==actor.id,
                AgentJob.requester_agent_id==lead.id,AgentJob.assignee_thread_id==thread.id,AgentJob.assignee_turn_id==turn.id,
                AgentJob.mode=='work',AgentJob.kind=='job',AgentJob.reply=='resume',AgentJob.status=='running'))
            if not job or thread.kind!='feature' or job.origin_kind!='person' or job.task_id is None:
                raise service.TeamError('outside_staff_scope','A current direct project assignment is required.')
            await checked_manager(db,owner,team,lead,{'_manager':actor.id})
            root=await db.get(AgentJob,job.parent_id)
            steward_chat=await db.get(Thread,job.requester_thread_id)
            if (not root or root.kind!='root' or root.assignee_thread_id!=job.requester_thread_id or
                    not steward_chat or steward_chat.kind!='chat' or steward_chat.agent_id!=lead.id or steward_chat.archived_at or
                    steward_chat.owner_id!=owner or steward_chat.room_id or steward_chat.job_id):
                raise service.TeamError('outside_staff_scope','The private steward conversation has changed.')
            if set(args)!={'name','title','instructions','first_task'} or not actor.model:
                raise service.TeamError('invalid_input','Supply the role, its instructions and initial task.')
            plan={'summary':f'{actor.name} 申请增加直属员工（只读）','operations':[{
                'op':'create',**args,'provider':actor.provider,'model':actor.model,
                'model_vendor':actor.model_vendor,'effort':actor.effort}]}
            setup._guided_plan(plan)
            plan=setup.redacted(plan)
            key='hire-'+setup.digest([owner,job.id,plan])[:42]
            row=await setup.existing(db,owner,key)
            if row:
                return {'request_id':key,'version':setup.request_version(row),'state':row.state}
            other=await db.scalar(select(TeamSetupRequest).where(TeamSetupRequest.owner_id==owner,TeamSetupRequest.team_id==team.id,
                TeamSetupRequest.kind=='setup',TeamSetupRequest.state=='pending',TeamSetupRequest.plan['_origin_job'].as_integer()==job.id))
            if other:
                raise service.TeamError('pending_hire','A hiring proposal already awaits review for this assignment.')
            operations=await setup.validate(db,owner,team,lead,plan,turn_id=turn.id,check_engine=rt.check_engine_model)
            card=setup.review_plan(plan,operations)
            row=TeamSetupRequest(owner_id=owner,team_id=team.id,lead_agent_id=lead.id,request_id=key,
                turn_id=job.requester_turn_id,thread_id=job.requester_thread_id,kind='setup',state='pending',
                plan={**card,'_manager':actor.id,'_origin_job':job.id,'_version':1},result={'version':1},undo={})
            db.add(row)
            await rt.team_jobs._deliver_to_holder(db,job,'hire:'+key)
            await thread_store.add_event_in_transaction(db,job.requester_thread_id,'team.setup.proposed',
                {'request_id':key,'version':1,'plan':card},turn_id=job.requester_turn_id)
            await db.commit()
            rt.hub.notify(job.requester_thread_id)
            rt.team_jobs.kick()
            return {'request_id':key,'version':1,'state':'pending'}
        if actor.id != lead.id or thread.kind!='chat' or thread.room_id or turn.person_id is not None:
            raise service.TeamError('lead_only','Only the private assigning steward can review this request.')
        key=args.get('request_id')
        if not isinstance(key,str) or args.get('decision') not in ('approve','reject','escalate') or type(args.get('version')) is not int:
            raise service.TeamError('invalid_input','Supply the exact request, version and decision.')
        row=await setup.existing(db,owner,key)
        if not row or row.lead_agent_id!=lead.id or not await summary(db,owner,key,thread.id) or setup.request_version(row)!=args['version']:
            raise service.TeamError('request_changed','This hiring request changed or has already been decided.')
        if args['decision']=='escalate' or (args['decision']=='approve' and not lead.can_staff_team):
            row.plan={**row.plan,'_owner_review':True}
            await db.commit()
            return {'request_id':key,'state':'pending_owner','note':'An owner review card is available in this conversation.'}
        if row.plan.get('_owner_review') or setup.request_version(row)!=1:
            raise service.TeamError('owner_review','The owner is reviewing this version.')
        if args['decision'] == 'approve':
            origin = await db.get(AgentJob, row.plan['_origin_job'], with_for_update=True)
            if (not origin or origin.status not in ('running', 'waiting', 'resume', 'needs_input')
                    or origin.owner_id != owner or origin.assignee_agent_id != row.plan['_manager']
                    or origin.requester_agent_id != lead.id or origin.requester_thread_id != thread.id):
                raise service.TeamError('outside_staff_scope', 'The original assignment is no longer active.')
        result = await setup.decide_pending(db,owner,key,args['version'],approve=args['decision']=='approve',check_engine=rt.check_engine_model)
        rt.team_jobs.kick()
        if result['state']=='applied':
            row=await setup.existing(db,owner,key)
            result['employee_id']=row.result['results'][0]['agent_id']
            result['manager_agent_id']=row.plan['_manager']
        return result
