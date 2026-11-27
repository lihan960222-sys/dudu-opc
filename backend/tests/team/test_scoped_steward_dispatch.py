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


def test_steward_cannot_turn_its_plan_into_mandatory_project_checks():
    from app.services.assignment_context import requested_output_check
    command = {"command": "pytest", "cwd": "."}
    path = {"scope": "role", "path": "out/result.txt"}
    proposal = {"kind": "commands", "commands": [{**command, "contains": "all passed"}], "files": [path]}
    assert requested_output_check(proposal, "看看这个软件能否启动，看到窗口再关闭。") == {"kind": "report"}
    assert requested_output_check(proposal, r"把结果存到 out\result.txt") == {"kind": "files", "files": [path]}
    assert requested_output_check(proposal, "先运行 pytest，再把结果存到 out/result.txt") == {
        "kind": "commands", "commands": [command], "files": [path]}
    assert requested_output_check(proposal, "pytest 输出 all passed，再写 out/result.txt") == proposal


async def test_steward_dispatch_carries_owner_goal_without_invented_command(sessions, owner, tmp_path, monkeypatch):
    from app.db.models import AgentJobEvent
    from app.services.assignment_context import employee_input
    from app.services.codex_adapter import CodexAdapter
    from app.services.thread_hub import ThreadHub
    monkeypatch.setattr(settings, "STEWARD_STRICT_OWNER", owner)
    monkeypatch.setenv("FEISHU_CONFIG", str(tmp_path / "no-feishu.json"))
    original = "打开软件，看到界面后再关闭。"
    async with sessions() as db:
        lead = await agent_store.create_agent(db, owner_id=owner, name="Lead", make_lead=True)
        worker = await agent_store.create_agent(db, owner_id=owner, name="Engineer", provider="codex")
        await service.change_member(db, owner, worker.id)
        worker.role_path = str(tmp_path)
        thread = await thread_store.create_thread(db, owner_id=owner, agent_id=lead.id, provider="codex")
        turn = await thread_store.start_turn(db, thread.id, original, origin="person")
    run = SimpleNamespace(agent_id=lead.id, turn_id=turn.id, thread_id=thread.id, owner_token=turn.owner_token,
                          ctx=SimpleNamespace(approval_mode="ask", team=SimpleNamespace(team_id=lead.team_id, origin="person")))
    jobs = Jobs(SimpleNamespace(session=sessions, owner_id=owner, hub=ThreadHub(), adapters={"codex": CodexAdapter()}))
    job = await jobs.enqueue(run, {"agent_id": worker.id, "project": "demo", "brief": "确认软件能正常打开，再关闭。",
                                   "feishu_delivery": "none",
                                   "completion_check": {"kind": "commands", "commands": [{"command": "invented.exe"}]}},
                             reply="resume")
    async with sessions() as db:
        messages, _ = await employee_input(db, job)
        assert messages == [{"text": original, "turn_id": turn.id}]
        check = await db.scalar(select(AgentJobEvent.detail).where(AgentJobEvent.job_id == job.id,
                                AgentJobEvent.kind == "completion_check"))
        assert check == {"kind": "report"}


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

def test_job_management_only_for_stewards_direct_owner_turn(tmp_path):
    from dataclasses import replace
    from app.team.mcp_server import Server
    from app.team.policy import TurnTeam, automatic
    lead = TurnTeam(1, 2, True, False, strict_hierarchy=True)
    tool = "mcp__team__manage_job"
    assert "manage_job" in {t["name"] for t in Server(tmp_path, steward=True).tools}
    assert "manage_job" not in {t["name"] for t in Server(tmp_path).tools}
    assert automatic(tool, "ask", lead, {})[0] == "allowed"
    for team in (replace(lead, origin="harness"), replace(lead, is_lead=False)):
        assert automatic(tool, "ask", team, {})[0] == "denied"
    assert automatic(tool, "plan", lead, {})[0] == "denied"
