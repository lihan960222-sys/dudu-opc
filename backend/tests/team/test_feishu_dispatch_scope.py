"""Feishu delivery is only required from the employee authorized for documents."""
from sqlalchemy import select

from app.core.config import settings
from app.db.models import Agent, AgentJob
from app.services.provider_adapter import TurnOutcome
from tests.services.thread_harness import cleanup, owner, sessions, wait_until  # noqa: F401
from tests.team.test_delegation import team, turns_of  # noqa: F401


async def _role_paths(sessions, tmp_path, *agents):
    async with sessions() as db:
        for agent in agents:
            row = await db.get(Agent, agent.id)
            row.role_path = str(tmp_path / f"role-{agent.id}")
        await db.commit()


def _configure(monkeypatch, tmp_path, rt, lead, roles, sources):
    from app.team import feishu_assignment
    state = tmp_path / "gateway"
    state.mkdir(exist_ok=True)
    (state / "channel.sqlite3").touch()
    cfg = {
        "owner_id": rt.owner_id,
        "steward": {"agent_id": lead.id, "app_id": "lead", "tenant_key": "tenant", "open_id": "owner"},
        "roles": roles,
        "state_dir": "gateway",
        "_access_config_path": str(tmp_path / "feishu.json"),
    }
    from app.services import engine_registry
    monkeypatch.setattr(engine_registry, "project_capability_gaps", lambda _: [])
    monkeypatch.setattr(settings, "STEWARD_STRICT_OWNER", rt.owner_id)
    monkeypatch.setattr(feishu_assignment, "configuration", lambda: cfg)
    monkeypatch.setattr(
        feishu_assignment.workspace_access,
        "settings_for",
        lambda _: {"history_sources": sources},
    )
    return cfg


async def test_feishu_turn_returns_missing_delivery_to_the_lead(team, sessions, monkeypatch, tmp_path):
    rt, engine, lead, employee, _ = team
    _configure(
        monkeypatch, tmp_path, rt, lead,
        {"二狗": {"agent_id": employee.id, "app_id": "employee"}},
        {"李晗": {"agent_id": employee.id, "oauth_app": "employee"}},
    )
    chat = await rt.create_thread(agent_id=lead.id)
    result = {}

    async def steward(ctx, sink, gate):
        run = rt._runs[ctx.turn_id]
        assert run.delivery_choices == (employee.id,)
        result.update(await rt.team_jobs.delegate(
            run,
            {"agent_ids": [employee.id], "brief": "整理和李晗的聊天记录"},
        ))
        return TurnOutcome("succeeded", result="我先确认这次飞书交付。")

    engine.handlers[chat.id] = steward
    await rt.send_turn(chat.id, "整理和李晗的聊天记录", direct_event_key="event-1")
    await wait_until(lambda: result)
    assert result["code"] == "delivery_choice"
    assert "none" in result["allowed"] and "create_document" in result["allowed"]
    async with sessions() as db:
        assert await db.scalar(select(AgentJob.id).where(AgentJob.owner_id == rt.owner_id)) is None
    await wait_until(lambda: not rt._runs)
    assert (await turns_of(sessions, chat.id))[-1].result == "我先确认这次飞书交付。"


async def test_retry_with_a_delivery_choice_delivers_after_the_lead_corrects_itself(team, sessions, monkeypatch, tmp_path):
    rt, engine, lead, employee, _ = team
    _configure(
        monkeypatch, tmp_path, rt, lead,
        {"二狗": {"agent_id": employee.id, "app_id": "employee"}},
        {"李晗": {"agent_id": employee.id, "oauth_app": "employee"}},
    )
    await _role_paths(sessions, tmp_path, employee)
    chat = await rt.create_thread(agent_id=lead.id)
    first, second = {}, {}

    async def steward(ctx, sink, gate):
        run = rt._runs[ctx.turn_id]
        first.update(await rt.team_jobs.delegate(
            run, {"agent_ids": [employee.id], "brief": "整理和李晗的聊天记录"}))
        second.update(await rt.team_jobs.delegate(
            run, {"agent_ids": [employee.id], "brief": "整理和李晗的聊天记录", "project": "验收项目",
                  "done_when": "整理完成", "feishu_delivery": "read"}))
        return TurnOutcome("succeeded", result="我先按本地资料整理。")

    engine.handlers[chat.id] = steward
    await rt.send_turn(chat.id, "整理和李晗的聊天记录", direct_event_key="event-2")
    await wait_until(lambda: second)
    assert first["code"] == "delivery_choice"
    assert second["receipts"][0].get("status") == "assigned", second


async def test_feishu_window_does_not_require_delivery_for_other_employees(team, sessions, monkeypatch, tmp_path):
    rt, engine, lead, employee, other = team
    _configure(
        monkeypatch, tmp_path, rt, lead,
        {"二狗": {"agent_id": employee.id, "app_id": "employee"},
         "Jensen": {"agent_id": other.id, "app_id": "other"}},
        {"李晗": {"agent_id": employee.id, "oauth_app": "employee"}},
    )
    await _role_paths(sessions, tmp_path, other)
    chat = await rt.create_thread(agent_id=lead.id)
    result = {}

    async def steward(ctx, sink, gate):
        run = rt._runs[ctx.turn_id]
        assert run.delivery_choices == (employee.id,)
        result.update(await rt.team_jobs.delegate(
            run,
            {"agent_ids": [other.id], "brief": "修复验收项目里失败的测试", "project": "验收项目",
             "done_when": "测试通过"},
        ))
        return TurnOutcome("succeeded", result="已交给 Jensen。")

    engine.handlers[chat.id] = steward
    await rt.send_turn(chat.id, "让 Jensen 修好项目", direct_event_key="event-3")
    await wait_until(lambda: result)
    assert result["receipts"][0].get("status") == "assigned", result


async def test_web_owner_turn_does_not_require_delivery_for_document_employee(team, sessions, monkeypatch, tmp_path):
    rt, engine, lead, employee, _ = team
    _configure(
        monkeypatch, tmp_path, rt, lead,
        {"二狗": {"agent_id": employee.id, "app_id": "employee"}},
        {"李晗": {"agent_id": employee.id, "oauth_app": "employee"}},
    )
    await _role_paths(sessions, tmp_path, employee)
    chat = await rt.create_thread(agent_id=lead.id)
    result = {}

    async def steward(ctx, sink, gate):
        run = rt._runs[ctx.turn_id]
        assert run.delivery_choices == ()
        result.update(await rt.team_jobs.delegate(
            run,
            {"agent_ids": [employee.id], "brief": "整理本地资料", "project": "验收项目",
             "done_when": "整理完成"},
        ))
        return TurnOutcome("succeeded", result="已交给二狗。")

    engine.handlers[chat.id] = steward
    await rt.send_turn(chat.id, "整理本地资料")
    await wait_until(lambda: result)
    assert result["receipts"][0].get("status") == "assigned", result

def test_delegate_schema_requires_an_explicit_delivery():
    from app.team.mcp_server import TOOLS
    delegate = next(tool for tool in TOOLS if tool["name"] == "delegate")
    assert "feishu_delivery" in delegate["inputSchema"]["required"]
