from types import SimpleNamespace as NS

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.models import AgentJob, AgentJobInbox
from app.services import agent_store, feature_tasks, thread_store
from app.team import service, steward_review
from app.team.jobs import Jobs
from tests.services.thread_harness import owner, sessions


@pytest.mark.parametrize("decision", ["allow", "deny", "escalate"])
@pytest.mark.asyncio
async def test_exact_write_is_reviewed_by_assigning_steward(sessions, owner, tmp_path, monkeypatch, decision):
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
            tool="feature_files.write_text",input={"path":"report.txt","text":"sample"})
        shown=await steward_review.preview(db,owner,job,ap)
        assert shown and shown["text"]=="sample"
        staff.approval_mode="ask"
        assert await steward_review.preview(db,owner,job,ap) is None
        staff.approval_mode="accept_edits"
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
