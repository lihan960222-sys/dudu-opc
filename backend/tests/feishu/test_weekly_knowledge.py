from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS

import pytest

from app.feishu import weekly_knowledge as weekly
from app.feishu.knowledge_revision import employee_blocks


def test_only_new_sources_join_the_prior_verified_topics():
    old = employee_blocks([{"title":"交付", "summary":"原定周五验收", "sources":["om_old"]}], {"om_old":{}}, "oc_chat")
    more = employee_blocks([{"title":"交付", "summary":"补充图纸已提交", "sources":["om_new"]}], {"om_new":{}}, "oc_chat")
    merged = weekly.merge_blocks(old, more, "oc_chat")
    assert merged[2:4] == old[2:4] and merged[-1] == more[-1]
    assert weekly.source_ids(merged, "oc_chat") == {"om_new", "om_old"}
    assert sum(row["block_type"] == 4 for row in merged) == 1
    with pytest.raises(ValueError):
        weekly.merge_blocks(old, old, "oc_chat")


def test_past_due_weeks_coalesce_without_replaying_each_missed_week():
    first = datetime(2026, 11, 2, 2, tzinfo=UTC)
    assert weekly.latest_due(first, first - timedelta(seconds=1)) is None
    assert weekly.latest_due(first, first + timedelta(days=18)) == first + timedelta(days=14)


def test_weekly_team_server_exposes_only_the_granted_feishu_tools(tmp_path):
    from app.team.bridge import TeamBridge
    from app.team.mcp_server import Server

    tools = ("feishu_read_history", "feishu_publish_knowledge")
    team = NS(feature_consult=True, delegation_enabled=False, scheduled_knowledge=True, feishu_tools=tools)
    bridge = TeamBridge(None, NS(turn_id=11, ctx=NS(team=team)))
    try:
        assert "--feishu-only" in bridge.launch.args
    finally:
        bridge.folder.rmdir()
    server = Server(tmp_path, escalations=True, project_consult=True, feishu_tools=tools,
                    employee_only=True, feishu_only=True)
    response = server.handle({"jsonrpc":"2.0", "id":1, "method":"tools/list"})
    assert {row["name"] for row in response["result"]["tools"]} == set(tools)
    assert server.call("enable_local_skill", {})["error"]


@pytest.mark.asyncio
async def test_unrelated_routine_cannot_acquire_the_weekly_grant():
    from unittest.mock import AsyncMock
    db = AsyncMock()
    db.get.return_value = None
    job = NS(origin_kind="routine", origin_ref="weekly-forged", id=3)
    assert await weekly.proof_for_job(db, {"owner_id":"owner"}, job) is None
    assert await weekly.proof_for_job(db, {}, NS(origin_kind="person")) is None


from tests.services.thread_harness import owner, sessions


@pytest.mark.asyncio
async def test_restart_claims_the_same_week_once_and_revocation_blocks_its_tool(sessions, owner, tmp_path, monkeypatch):
    from app.db.models import AgentJob
    from app.db.models.knowledge_weekly import KnowledgeWeeklyGrant, KnowledgeWeeklySlot
    from app.services import agent_store, feature_tasks, thread_store
    moment = datetime(2026, 11, 26, 1, tzinfo=UTC)
    async with sessions() as db:
        lead = await agent_store.create_agent(db, owner_id=owner, name="Lead", make_lead=True)
        employee = await agent_store.create_agent(db, owner_id=owner, name="Worker")
        thread = await thread_store.create_thread(db, owner_id=owner, agent_id=lead.id, provider="codex", workspace_path=str(tmp_path))
        turn = await thread_store.start_turn(db, thread.id, "Weekly grant from owner")
        task = (await feature_tasks.route(db, owner_id=owner, agent_id=employee.id, project="P", event_key="initial",
                                          title="Knowledge", text="Publish"))["task_id"]
        source = AgentJob(owner_id=owner, requester_agent_id=lead.id, requester_thread_id=thread.id,
                          requester_turn_id=turn.id, assignee_agent_id=employee.id, task_id=task, project="P",
                          brief="Initial knowledge task", brief_hash="mock", status="completed")
        db.add(source)
        await db.flush()
        scope = {"lead":lead.id,"employee":employee.id,"contact":"联系人","vendor":"openai",
                 "document":"doc","destination":{"tenant_origin":"https://example.feishu.cn"}}
        db.add(KnowledgeWeeklyGrant(owner_id=owner, employee_id=employee.id, task_id=task, assignment_epoch=1,
                                    source_job_id=source.id, enabled=True, scope=scope, generation="one", next_due=moment))
        await db.commit()
    async def live(*args):
        return None, NS(project="P"), scope
    monkeypatch.setattr(weekly, "_live", live)
    monkeypatch.setenv("SUPARAGENT_WEEKLY_KNOWLEDGE_ENABLED", "1")
    cfg = {"owner_id":owner}
    async with sessions() as db:
        job_id = await weekly.claim(db, cfg, employee.id, moment)
    async with sessions() as db:
        assert not await weekly.claim(db, cfg, employee.id, moment)
        job = await db.get(AgentJob, job_id)
        assert job.origin_kind == "routine" and job.requester_turn_id == turn.id and job.task_id == task
        slot = await db.get(KnowledgeWeeklySlot, job.origin_ref)
        assert slot.job_id == job_id and (await weekly.proof_for_job(db,cfg,job))["contact"] == "联系人"
        grant = await db.get(KnowledgeWeeklyGrant, (owner, employee.id))
        grant.enabled = False
        await db.commit()
        assert await weekly.proof_for_job(db,cfg,job) is None
