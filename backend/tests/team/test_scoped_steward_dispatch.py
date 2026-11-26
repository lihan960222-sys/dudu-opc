import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.models import AgentJob
from app.services import agent_store, thread_store
from app.team import policy, prompt, service
from app.team.jobs import Jobs
from app.team.mcp_server import Server
from tests.services.thread_harness import owner, sessions  # noqa: F401


async def test_scoped_steward_offers_real_employee_work_even_for_short_questions(
        sessions, owner, tmp_path, monkeypatch):
    from app.services.thread_runtime import TeamBridge
    monkeypatch.setattr(settings, "STEWARD_STRICT_OWNER", owner)
    async with sessions() as db:
        lead = await agent_store.create_agent(db, owner_id=owner, name="Lead", make_lead=True)
        team = await service.team_for(db, owner)
        instructions, _ = await prompt.parts(db, team=team, agent=lead, owner_id=owner)
    ctx = SimpleNamespace(team=policy.TurnTeam(team.id, lead.id, True, True, strict_hierarchy=True))
    bridge = TeamBridge(SimpleNamespace(owner_id=owner), SimpleNamespace(ctx=ctx, turn_id=1))
    try:
        assert "--steward" in bridge.launch.args
    finally:
        await bridge.close()
    tools = {t["name"]: t for t in Server(tmp_path, steward=True).tools}
    assert "ask_agent" not in tools
    assert tools["delegate"]["inputSchema"]["properties"]["mode"]["enum"] == ["work"]
    assert "Use ask_agent" not in instructions
    assert any(t["name"] == "ask_agent" for t in Server(tmp_path).tools)
    assert any(t["name"] == "ask_agent" for t in Server(tmp_path, project_consult=True).tools)


@pytest.mark.parametrize(("mode", "reply"), [("consult", "wait"), ("consult", "resume"), ("work", "wait")])
async def test_incompatible_dispatch_does_not_persist_an_unstartable_job(
        sessions, owner, monkeypatch, mode, reply):
    monkeypatch.setattr(settings, "STEWARD_STRICT_OWNER", owner)
    async with sessions() as db:
        lead = await agent_store.create_agent(db, owner_id=owner, name="Lead", make_lead=True)
        employee = await agent_store.create_agent(db, owner_id=owner, name="Engineer")
        await service.change_member(db, owner, employee.id)
        team = await service.team_for(db, owner)
        thread = await thread_store.create_thread(db, owner_id=owner, agent_id=lead.id, provider="codex")
        turn = await thread_store.start_turn(db, thread.id, "Have the engineer check their own folder.")
        ctx = SimpleNamespace(approval_mode="ask",
                              team=policy.TurnTeam(team.id, lead.id, True, True, strict_hierarchy=True))
        run = SimpleNamespace(ctx=ctx, agent_id=lead.id, turn_id=turn.id,
                              thread_id=thread.id, owner_token=turn.owner_token)
        employee_id = employee.id
        await db.commit()
    with pytest.raises(service.TeamError) as exc:
        await Jobs(SimpleNamespace(session=sessions, owner_id=owner)).enqueue(
            run, {"agent_id": employee_id, "mode": mode, "brief": "Check your folder."}, reply=reply)
    assert exc.value.code == "use_scoped_delegate"
    async with sessions() as db:
        assert not await db.scalar(select(AgentJob.id).where(AgentJob.owner_id == owner))


async def test_steward_receipt_carries_the_original_request_only_to_its_own_thread(sessions, owner, monkeypatch):
    monkeypatch.setattr(settings, "STEWARD_STRICT_OWNER", owner)
    async with sessions() as db:
        lead = await agent_store.create_agent(db, owner_id=owner, name="Lead", make_lead=True)
        thread = await thread_store.create_thread(db, owner_id=owner, agent_id=lead.id, provider="codex")
        other = await thread_store.create_thread(db, owner_id=owner, agent_id=lead.id, provider="codex")
        original = "Please give two examples from this folder."
        turn = await thread_store.start_turn(db, thread.id, original)
        holder = SimpleNamespace(id=100, restart=None, task_id=None, kind="root",
                                 requester_turn_id=turn.id, requester_thread_id=thread.id, assignee_thread_id=thread.id)
        jobs = Jobs(SimpleNamespace(session=sessions, owner_id=owner))
        result = await jobs._resume_text(db, holder, [])
        assert json.loads(result.rsplit("\n", 1)[1])["owner_request"] == original
        holder.assignee_thread_id = other.id
        assert original not in await jobs._resume_text(db, holder, [])
