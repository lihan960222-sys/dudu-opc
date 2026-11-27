from types import SimpleNamespace as NS

import pytest
from app.core.config import settings
from app.auth import people
from app.db.models import AgentJob
from app.services import agent_store, thread_store
from app.services.codex_adapter import CodexAdapter
from app.services.thread_hub import ThreadHub
from app.team import service
from app.team.jobs import Jobs
from tests.services.thread_harness import owner, sessions


async def test_steward_can_stop_and_archive_without_faking_delivery(sessions, owner, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STEWARD_STRICT_OWNER", owner)
    monkeypatch.setenv("FEISHU_CONFIG", str(tmp_path / "no-feishu.json"))
    async with sessions() as db:
        lead = await agent_store.create_agent(db, owner_id=owner, name="Lead", make_lead=True)
        employee = await agent_store.create_agent(db, owner_id=owner, name="Employee", provider="codex")
        await service.change_member(db, owner, employee.id)
        employee.role_path = str(tmp_path)
        chat = await thread_store.create_thread(db, owner_id=owner, agent_id=lead.id, provider="codex")
        turn = await thread_store.start_turn(db, chat.id, "Review the folder", origin="person")
    run = NS(agent_id=lead.id, thread_id=chat.id, turn_id=turn.id, owner_token=turn.owner_token,
             ctx=NS(approval_mode="ask", team=NS(team_id=lead.team_id, origin="person")))
    jobs = Jobs(NS(session=sessions, owner_id=owner, hub=ThreadHub(), adapters={"codex": CodexAdapter()}, _runs={}))
    monkeypatch.setattr(jobs, "kick", lambda: None)
    job = await jobs.enqueue(run, {"agent_id": employee.id, "project": "demo", "brief": "Review folder",
                                   "feishu_delivery": "none"}, reply="resume")
    delivered = await jobs.enqueue(run, {"agent_id": employee.id, "project": "other", "brief": "Report version",
                                         "feishu_delivery": "none"}, reply="resume")
    await jobs.settle(delivered.id, "completed", "done", result="Version verified")
    async with sessions() as db:
        chat2 = await thread_store.create_thread(db, owner_id=owner, agent_id=lead.id, provider="codex")
        followup = await thread_store.start_turn(db, chat2.id, "Stop all tasks", origin="person")
    run.thread_id, run.turn_id, run.owner_token = chat2.id, followup.id, followup.owner_token
    with pytest.raises(service.TeamError, match="需要老板"):
        await jobs.manage_job(run, {"action":"stop", "all":True})
    async with sessions() as db:
        from app.db.models import Turn
        (await db.get(Turn, followup.id)).person_id = (await people.ensure_owner_person(db)).id
        await db.commit()
    with pytest.raises(service.TeamError):
        await jobs.manage_job(run, {"action":"complete", "job_id": job.id})
    assert (await jobs.manage_job(run, {"action":"complete", "job_id":delivered.id}))["completed"]
    result = await jobs.manage_job(run, {"action":"stop", "all":True})
    assert result["jobs"] == [{"job_id": job.id, "task_id": job.task_id, "status":"cancelled"}]
    assert (await jobs.manage_job(run, {"action":"stop", "all":True}))["jobs"] == []
    result = await jobs.manage_job(run, {"action":"archive", "job_id":job.id})
    assert result["archived"] and not result["completed"]
    async with sessions() as db:
        assert (await db.get(AgentJob,job.root_id)).status == "cancelled"
