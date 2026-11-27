import asyncio
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

from app.auth import people
from app.core.config import settings
from app.db.models import AgentJob
from app.services import agent_store, employee_approval_rules, employee_native_review, employee_runtime, feature_tasks, thread_store
from app.team import service, steward_review
from tests.services.thread_harness import cleanup_jobs, owner, sessions  # noqa: F401


def project_at(tmp_path, owner='owner', employee=3, task=17):
    return employee_runtime.ProjectRuntime(owner, employee, task, 1, 'openai', tmp_path, True, (),
        profile_path=tmp_path/'control'/'runtime'/'owners'/'3'/'openai', local_workstation=True)


def test_native_requests_use_effective_grants_across_projects_and_protocol_shapes(tmp_path):
    project = project_at(tmp_path)
    source = tmp_path/'reports'
    source.mkdir()
    def grant(permissions):
        return {'cwd':str(tmp_path), 'permissions':permissions}
    old = grant({'fileSystem':{'read':[str(source)]}})
    typed = grant({'fileSystem':{'entries':[{'access':'read','path':{'type':'path','path':str(source)}}]}})
    identity = employee_approval_rules.identity(project,'codex.request_permissions',old)
    assert identity and employee_approval_rules.identity(project,'codex.request_permissions',typed) == identity
    for denied in (grant({'fileSystem':{'write':[str(tmp_path.parent)]}}),
                   grant({'fileSystem':{'entries':[{'access':'read','path':{'type':'special','value':{'kind':'root'}}}]}}),
                   grant({'fileSystem':{'read':[str(tmp_path/'.env')]}}), grant({'network':{'enabled':True}})):
        assert employee_native_review.describe(project,'codex.request_permissions',denied)['delegated_scope'] is None
    online = replace(project, outbound_network=True)
    assert employee_native_review.describe(online,'codex.request_permissions',grant({'network':{'enabled':True}}))['delegated_scope']['network']
    patch = {'changes':[{'path':'result.txt','kind':{'type':'update','move_path':'report.txt'},'diff':'sample change'}]}
    assert len(employee_native_review.describe(project,'apply_patch',patch)['delegated_scope']['project_files']) == 2
    patch['changes'][0]['kind']['move_path'] = str(tmp_path.parent/'elsewhere.txt')
    assert employee_native_review.describe(project,'apply_patch',patch)['delegated_scope'] is None
    opaque = {'cwd':str(tmp_path),'command':'run-unchecked-helper --apply',
              'commandActions':[{'type':'listFiles','command':'list','path':str(tmp_path)}]}
    assert employee_native_review.describe(project,'Bash',opaque)['delegated_scope'] is None
    # A new command is eligible without adding a command-name rule. Only the
    # host's bounded permission overlay changes the authority.
    bounded = {**opaque, 'command':'image-inspector --measure sample.png', 'additionalPermissions':old['permissions']}
    assert employee_native_review.describe(project,'Bash',bounded)['delegated_scope']['bounded_command']
    assert employee_native_review.describe(project,'Bash',{**bounded,'kind':'humanCommand'}) is None
    bounded['additionalPermissions'] = {'fileSystem':{'write':[str(tmp_path.parent)]}}
    assert employee_native_review.describe(project,'Bash',bounded)['delegated_scope'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('nested',[False,True])
async def test_real_pending_request_reaches_steward_once_and_rechecks_before_delivery(sessions, owner, cleanup_jobs, tmp_path, monkeypatch, nested):
    from app.team.jobs import Jobs
    monkeypatch.setattr(settings,'STEWARD_STRICT_OWNER',owner)
    # Construct the journal without consulting or starting an installed engine.
    monkeypatch.setattr(employee_runtime,'native_enabled',lambda *_:False)
    async with sessions() as db:
        lead = await agent_store.create_agent(db,owner_id=owner,name='Lead',provider='codex',make_lead=True)
        staff = await agent_store.create_agent(db,owner_id=owner,name='Editor',provider='codex',approval_mode='accept_edits')
        await service.change_member(db,owner,staff.id)
        staff.role_path = str(tmp_path)
        boss = await people.ensure_owner_person(db)
        chat = await thread_store.create_thread(db,owner_id=owner,agent_id=lead.id,provider='codex',workspace_path=str(tmp_path))
        source = await thread_store.start_turn(db,chat.id,'Check the reports folder',person_id=boss.id,origin='person',use_employee_role=True)
        await thread_store.finish_turn(db,source.id,source.owner_token,status='succeeded',result='Assigned')
        reviewer = await thread_store.start_turn(db,chat.id,'Review pending employee operations',internal=True)
        card = (await feature_tasks.route(db,owner_id=owner,agent_id=staff.id,project='reports',event_key='source',title='Audit',text='Check'))['task_id']
        thread,_ = await feature_tasks.session_for(db,owner,card,model='gpt-unit',workspace_path=str(tmp_path))
        job_id = await feature_tasks.dispatch(db,owner_id=owner,steward_id=lead.id,task_id=card,event_key='source',thread_id=thread.id)
        project = project_at(tmp_path, owner, staff.id, card)
        thread.provider_state = {'employee_project':project.stamp}
        turn = await thread_store.start_turn(db,thread.id,'Check',feature_event_key='source')
        job = await db.get(AgentJob,job_id)
        holder = await db.get(AgentJob,job.root_id or job.parent_id)
        holder.assignee_thread_id = holder.requester_thread_id = chat.id
        holder.requester_turn_id, holder.status, holder.current_turn_id = source.id, 'running', reviewer.id
        job.requester_thread_id, job.requester_turn_id = chat.id, source.id
        job.status, job.reply, job.current_turn_id, job.approval_mode = 'running','resume',turn.id,'accept_edits'
        if nested:
            manager = await agent_store.create_agent(db,owner_id=owner,name='Supervisor',provider='codex',approval_mode='accept_edits')
            await service.change_member(db,owner,manager.id)
            manager.manager_agent_id, manager.delegation_enabled, manager.delegate_project_writes = lead.id,True,True
            staff.manager_agent_id = manager.id
            assigned = (await feature_tasks.route(db,owner_id=owner,agent_id=manager.id,project='reports',event_key='parent-source',title='Review work',text='Check'))['task_id']
            mchat,_ = await feature_tasks.session_for(db,owner,assigned,model='gpt-unit',workspace_path=str(tmp_path))
            mturn = await thread_store.start_turn(db,mchat.id,'Delegate review')
            parent = AgentJob(owner_id=owner,kind='job',mode='work',reply='resume',depth=1,origin_kind='person',
                root_id=holder.id,parent_id=holder.id,task_id=assigned,project='reports',assignment_epoch=1,status='waiting',
                requester_agent_id=lead.id,requester_thread_id=chat.id,requester_turn_id=source.id,assignee_agent_id=manager.id,
                assignee_thread_id=mchat.id,assignee_turn_id=mturn.id,approval_mode='accept_edits',brief='Audit',brief_hash='test')
            db.add(parent)
            await db.flush()
            job.parent_id, job.depth, job.requester_agent_id = parent.id,2,manager.id
            job.requester_thread_id, job.requester_turn_id = mchat.id,mturn.id
        args = {'permissions':{'fileSystem':{'read':[str(tmp_path/'reports')]}},'cwd':str(tmp_path)}
        request = await thread_store.create_approval(db,thread_id=thread.id,turn_id=turn.id,request_id='native-1',
            tool='codex.request_permissions',input=args,owner_token=turn.owner_token)
        await db.commit()

    async def current(*_args):
        return project
    monkeypatch.setattr(feature_tasks,'project_runtime_for',current)
    monkeypatch.setattr(employee_runtime,'checked_active_project',current)
    gate = NS(_deadlines={request.id:asyncio.get_running_loop().time()+60},employee_rule_keys={})
    target = NS(thread_id=thread.id,turn_id=turn.id,owner_token=turn.owner_token,stop_requested=False,gate=gate,
                ctx=NS(feature_workspace=str(tmp_path),project_runtime=project,approval_mode='accept_edits',aborted=False))
    lead_run = NS(thread_id=chat.id, turn_id=reviewer.id, owner_token=reviewer.owner_token, agent_id=lead.id,ctx=NS(approval_mode='ask'))
    rt = NS(session=sessions,owner_id=owner,_runs={turn.id:target},_waiters={},hub=NS(notify=lambda _:None))
    jobs = Jobs(rt)
    await jobs.reconcile()
    async with sessions() as db:
        from sqlalchemy import select
        from app.db.models import AgentJobInbox
        inbox = list(await db.scalars(select(AgentJobInbox).where(AgentJobInbox.kind=='review:'+request.id)))
        assert len(inbox)==1 and inbox[0].holder_id==holder.id
        assert request.id in await jobs._resume_text(db,await db.get(AgentJob,holder.id),inbox)
        details = await steward_review.preview(db,owner,await db.get(AgentJob,job.id),await thread_store.get_approval(db,request.id))
        assert details and details['owner_request'] == 'Check the reports folder' and details['delegated_scope']
        if nested:
            live_manager = await db.get(type(manager),manager.id)
            live_manager.delegate_project_writes = False
            assert not await steward_review.preview(db,owner,await db.get(AgentJob,job.id),await thread_store.get_approval(db,request.id))
            await db.rollback()
    decision = {**details,'decision':'allow','reason':'The requested report inspection needs these files.'}
    with pytest.raises(service.TeamError,match='no longer pending'):
        await steward_review.decide(rt,lead_run,{**decision,'request_digest':'another'},native=True)
    assert (await steward_review.decide(rt,lead_run,decision,native=True))['status'] == 'allowed'
    with pytest.raises(service.TeamError):
        await steward_review.decide(rt,lead_run,decision,native=True)
    async with sessions() as db:
        approved = await thread_store.get_approval(db,request.id)
        live = await db.get(AgentJob,job.id)
        assert await steward_review.recheck_native_decision(db,live,target,approved) == (True,None)
        assert not await steward_review.history(db,live,authority_profile=details['authority_profile'])
        assert await thread_store.mark_allow_delivered(db,request.id,turn.owner_token)
        assert (await steward_review.history(db,live,authority_profile=details['authority_profile']))[0]['reason'] == decision['reason']
        assert not await steward_review.history(db,live,authority_profile='grant-has-changed')
        live.approval_mode = 'plan'
        assert await steward_review.recheck_native_decision(db,live,target,approved) == (False,None)
        await db.rollback()

    from app.services.provider_adapter import ApprovalHandle
    from app.services.thread_runtime import ThreadApprovalGate
    target.gate = ThreadApprovalGate(rt,target)
    for attempt in range(2):
        rid = f'narrow-{attempt}'
        async with sessions() as db:
            pending = await thread_store.create_approval(db,thread_id=thread.id,turn_id=turn.id,request_id=rid,
                tool='Bash',input={'cwd':str(tmp_path),'command':'sample-helper --all'},owner_token=turn.owner_token)
            details = await steward_review.preview(db,owner,await db.get(AgentJob,job.id),pending)
        assert details and details['delegated_scope'] is None
        target.gate._deadlines[pending.id] = asyncio.get_running_loop().time()+40
        rt._waiters[pending.id] = asyncio.get_running_loop().create_future()
        status = await steward_review.decide(rt,lead_run,{**details,'decision':'request_narrower','reason':'Only the input folder is needed.'},native=True)
        if attempt == 0:
            assert status['status'] == 'denied'
            reply = await target.gate.decide(ApprovalHandle(approval_id=pending.id,request_id=rid,tool='Bash'))
            assert reply.kind == 'deny' and reply.message == employee_native_review.NARROW_REQUEST_HINT
        else:
            assert status['status'] == 'pending_owner' and not rt._waiters[pending.id].done()
