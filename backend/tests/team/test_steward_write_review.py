from types import SimpleNamespace as NS

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.models import AgentJob, AgentJobEvent, AgentJobInbox
from app.services import agent_store, feature_tasks, thread_store
from app.team import service, steward_review
from app.team.jobs import Jobs
from tests.services.thread_harness import cleanup_jobs, owner, sessions


@pytest.mark.asyncio
async def test_owner_decision_resumes_same_worker_via_lead(sessions, owner, cleanup_jobs, tmp_path):
    import asyncio
    from app.services.thread_runtime import ThreadRuntime
    async with sessions() as db:
        lead = await agent_store.create_agent(db, owner_id=owner, name="Lead", provider="codex", make_lead=True)
        worker = await agent_store.create_agent(db, owner_id=owner, name="Worker", provider="codex", approval_mode="ask")
        await service.change_member(db, owner, worker.id)
        card = (await feature_tasks.route(db, owner_id=owner, agent_id=worker.id, project="P", event_key="one",
                                          title="Work", text="Check"))["task_id"]
        thread, _ = await feature_tasks.session_for(db, owner, card, model="gpt-unit", workspace_path=str(tmp_path))
        thread.provider_state = {"employee_project":{"employee":worker.id}}
        turn = await thread_store.start_turn(db, thread.id, "Check")
        db.add(AgentJob(owner_id=owner, kind="job", task_id=card, project="P", assignment_epoch=1,
            requester_thread_id=thread.id, requester_turn_id=turn.id, requester_agent_id=lead.id,
            assignee_thread_id=thread.id, assignee_turn_id=turn.id, current_turn_id=turn.id, assignee_agent_id=worker.id,
            status="running", mode="work", brief="Check", brief_hash="mock", approval_mode="ask"))
        approval = await thread_store.create_approval(db, thread_id=thread.id, turn_id=turn.id, request_id="one",
            tool="mcp__community__lookup", input={"query":"example"}, owner_token=turn.owner_token)
    loop = asyncio.get_running_loop()
    waiter = loop.create_future()
    project = NS(owner_id=owner, employee_id=worker.id, task_id=card, assignment_epoch=1, writable=True)
    run = NS(thread_id=thread.id, turn_id=turn.id, owner_token=turn.owner_token, stop_requested=False,
             gate=NS(_deadlines={approval.id:loop.time()+30}),
             ctx=NS(feature_workspace=str(tmp_path), project_runtime=project, aborted=False, approval_mode="ask"))
    rt = NS(session=sessions, _runs={turn.id:run}, _waiters={approval.id:waiter}, _mode_of=lambda _: "ask",
            hub=NS(notify=lambda _:None))
    args = {"expected_turn_id":turn.id, "expected_assignment_epoch":1, "expected_employee_id":worker.id}
    assert not await ThreadRuntime.answer_approval(rt, approval.id, "allow", **{**args,"expected_assignment_epoch":2})
    assert await ThreadRuntime.answer_approval(rt, approval.id, "allow", **args)
    assert await waiter == "allowed"
    assert not await ThreadRuntime.answer_approval(rt, approval.id, "allow", **args)


@pytest.mark.asyncio
async def test_nested_writes_need_a_current_owner_grant(sessions, owner):
    async with sessions() as db:
        manager = await agent_store.create_agent(db, owner_id=owner, name="Manager", approval_mode="accept_edits")
        child = await agent_store.create_agent(db, owner_id=owner, name="Child", approval_mode="accept_edits")
        manager.team_id = child.team_id = 77
        child.manager_agent_id = manager.id
        parent = NS(owner_id=owner, assignee_agent_id=manager.id, kind="job", mode="work", task_id=None,
                    status="running", origin_kind="person", approval_mode="ask")
        assert await steward_review.delegated_mode(db,parent,manager,child,"accept_edits") == "plan"
        manager.delegate_project_writes = True
        assert await steward_review.delegated_mode(db,parent,manager,child,"accept_edits") == "ask"
        child.approval_mode = "plan"
        assert await steward_review.delegated_mode(db,parent,manager,child,"accept_edits") == "plan"


@pytest.mark.parametrize("decision", ["allow", "deny", "escalate"])
@pytest.mark.asyncio
async def test_exact_write_is_reviewed_by_assigning_steward(sessions, owner, cleanup_jobs, tmp_path, monkeypatch, decision):
    monkeypatch.setattr(settings,"STEWARD_STRICT_OWNER",owner)
    async with sessions() as db:
        lead = await agent_store.create_agent(db, owner_id=owner, name="Lead", provider="codex", make_lead=True)
        staff = await agent_store.create_agent(db, owner_id=owner, name="Worker", provider="codex", approval_mode="accept_edits")
        await service.change_member(db,owner,staff.id)
        staff.role_path=str(tmp_path)
        lead_thread=await thread_store.create_thread(db,owner_id=owner,agent_id=lead.id,provider="codex",workspace_path=str(tmp_path))
        lead_turn=await thread_store.start_turn(db,lead_thread.id,"review")
        card=(await feature_tasks.route(db,owner_id=owner,agent_id=staff.id,project="P",event_key="a",title="A",text="A"))["task_id"]
        root=tmp_path/"任务"/str(card)
        root.mkdir(parents=True)
        thread,_=await feature_tasks.session_for(db,owner,card,model="m",workspace_path=str(root))
        job_id=await feature_tasks.dispatch(db,owner_id=owner,steward_id=lead.id,task_id=card,event_key="a",thread_id=thread.id)
        turn=await thread_store.start_turn(db,thread.id,"A",feature_event_key="a")
        job=await db.get(AgentJob,job_id)
        parent=await db.get(AgentJob,job.parent_id)
        parent.assignee_thread_id=parent.requester_thread_id=lead_thread.id
        parent.status="waiting"
        job.requester_thread_id,job.requester_turn_id=lead_thread.id,lead_turn.id
        job.status,job.reply,job.current_turn_id="running","resume",turn.id
        ap=await thread_store.create_approval(db,thread_id=thread.id,turn_id=turn.id,request_id="w",
            tool="feature_files.write_text",input={"path":"结果.txt","text":"结果已核对，待领取。"})
        shown=await steward_review.preview(db,owner,job,ap)
        assert shown and shown["text"]=="结果已核对，待领取。"
        staff.approval_mode="ask"
        assert await steward_review.preview(db,owner,job,ap) is None
        staff.approval_mode="accept_edits"
        staff.manager_agent_id=staff.id
        assert await steward_review.preview(db,owner,job,ap) is None
        staff.manager_agent_id=lead.id
        await db.commit()

    run=NS(agent_id=lead.id,thread_id=lead_thread.id,turn_id=lead_turn.id,owner_token=lead_turn.owner_token,ctx=NS(approval_mode="ask"))
    employee=NS(turn_id=turn.id,ctx=NS(feature_workspace=str(root),approval_mode="ask",aborted=False),stop_requested=False,owner_token=turn.owner_token)
    rt=NS(session=sessions,owner_id=owner,_runs={turn.id:employee},_waiters={},hub=NS(notify=lambda _:None))
    jobs=Jobs(rt)
    await jobs.reconcile()
    await jobs.reconcile()
    async with sessions() as db:
        reviews=list(await db.scalars(select(AgentJobInbox).where(AgentJobInbox.kind=="review:"+ap.id)))
        assert len(reviews)==1
        text=await jobs._resume_text(db,await db.get(AgentJob,parent.id),reviews)
        assert "review_employee_write" in text and shown["sha256"] in text
    args={**shown,"decision":decision,"reason":"Requested report in the task directory"}
    with pytest.raises(service.TeamError, match="no longer pending"):
        await steward_review.decide(rt,run,{**args,"sha256":"incorrect"})
    assert (await steward_review.decide(rt,run,args))["status"] == {"allow":"allowed","deny":"denied","escalate":"pending_owner"}[decision]
    with pytest.raises(service.TeamError):
        await steward_review.decide(rt,run,args)
    async with sessions() as db:
        if decision == "escalate":
            assert (await thread_store.get_approval(db,ap.id)).status == "pending"
            await thread_store.decide_approval(db,ap.id,"denied")
            record = (await steward_review.history(db,job))[0]
            assert record["source"] == "person" and record["escalated"] and record["decision"] == "denied"
        else:
            assert (await steward_review.history(db,job))[0]["source"]=="steward"

    async with sessions() as db:
        delivered = await thread_store.mark_allow_delivered(db,ap.id,turn.owner_token)
        assert delivered == (decision == "allow")
        if delivered:
            from app.services.feature_files import FeatureFiles
            FeatureFiles(root).call("write_text",ap.input)
            assert (root/"结果.txt").read_text("utf-8") == "结果已核对，待领取。"
            assert not await thread_store.mark_allow_delivered(db,ap.id,turn.owner_token)
        else:
            assert not (root/"结果.txt").exists()
        event = await db.scalar(select(AgentJobEvent).where(AgentJobEvent.job_id==job.id,
            AgentJobEvent.kind.in_(("steward_review","review_escalated"))))
        assert event.detail["approval_id"] == ap.id and event.detail["decision"] == decision
