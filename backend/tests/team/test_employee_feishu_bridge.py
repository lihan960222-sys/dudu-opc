from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.team import feishu_tools_bridge as bridge, mcp_server, policy


def test_catalog_requires_job_actions_and_employee_oauth(monkeypatch,tmp_path):
    cfg = {"steward":{"agent_id":1}}
    pin = {"employee":4,"contact":"李晗","action":"employee"}
    monkeypatch.setattr(bridge.owner_oauth, "employee_config", lambda cfg, employee:
        {"_employee_oauth":True,"steward":{"app_id":"worker"}})
    actions = {"history":True,"doc":True,"folder":True}
    monkeypatch.setattr(bridge.owner_oauth,"status",lambda cfg:{"actions":actions})
    monkeypatch.setattr(bridge.workspace_access,"settings_for",lambda cfg:{
        "history_sources":{"李晗":{"agent_id":4,"oauth_app":"worker"}},"allow_codex_summary":True})
    monkeypatch.setattr(bridge.knowledge_document,"target",lambda *args:({}, "知识库"))
    assert "feishu_publish_knowledge" in bridge.configured(cfg,4,pin)[1]
    pin["expected_tool"] = "feishu_read_history"
    assert "feishu_publish_knowledge" not in bridge.configured(cfg,4,pin)[1]
    pin["expected_tool"] = "feishu_publish_knowledge"
    _, tools = bridge.configured(cfg,4,pin)
    team = policy.TurnTeam(1,4,False,False,feature_consult=True,feishu_tools=tools)
    request = {"jsonrpc":"2.0","method":"tools/list","id":1}
    server = mcp_server.Server(tmp_path, project_consult=True, feishu_tools=tools,employee_only=True)
    names = [t["name"] for t in server.handle(request)["result"]["tools"]]
    assert names == ["employee_capabilities","enable_local_skill","feishu_list_sources","feishu_read_history","feishu_read_document","feishu_read_message","feishu_publish_knowledge"]
    from app.services.codex_adapter import feature_team_tools
    assert set(names)==set(feature_team_tools(SimpleNamespace(team=SimpleNamespace(feishu_tools=tools,delegation_enabled=False))))
    descriptions={t["name"]:t["description"] for t in server.handle(request)["result"]["tools"]}
    assert "same-title" in descriptions["feishu_list_sources"] and "creates one" in descriptions["feishu_publish_knowledge"]
    for name in names:
        assert policy.automatic("mcp__team__"+name,"ask",team,{})[0]=="allowed"
    assert policy.automatic("mcp__team__feishu_create_document","ask",team,{})[0]=="denied"
    assert not any(t["name"].startswith("feishu_") for t in mcp_server.Server(tmp_path).tools)
    assert bridge.configured(cfg,5,pin) is None
    assert "feishu_publish_knowledge" not in bridge.configured(cfg,4,{**pin,"read_only":True})[1]
    actions["folder"] = False
    blocked, read_tools = bridge.configured(cfg,4,pin)
    assert "space:document:retrieve" in blocked["delivery_blocker"]
    assert "feishu_list_sources" in read_tools and "feishu_publish_knowledge" not in read_tools
    actions["folder"] = True
    actions["doc"] = False
    assert "feishu_publish_knowledge" not in bridge.configured(cfg,4,pin)[1]
    actions["history"] = False
    assert bridge.configured(cfg,4,pin) is None


@pytest.mark.asyncio
async def test_disallow_lead_other_turn_and_other_job():
    ctx = SimpleNamespace(approval_mode="ask")
    team = SimpleNamespace(lead_agent_id=1,owner_id="owner")
    run = SimpleNamespace(agent_id=1,turn_id=21,thread_id=5,owner_token="current",ctx=ctx)
    db=AsyncMock()
    assert await bridge.grant(db,"owner",run,team) is None
    db.get.assert_not_called()
    run.agent_id = 4
    db.get.return_value = SimpleNamespace(status="running",owner_token="stale",thread_id=5)
    assert await bridge.grant(db,"owner",run,team) is None
    db.scalar.assert_not_called()
    db.get.return_value.owner_token="current"
    db.scalar.return_value=None
    assert await bridge.grant(db,"owner",run,team) is None


@pytest.mark.asyncio
async def test_authorized_employee_call_reaches_handler_and_records_receipt(monkeypatch, tmp_path):
    from contextlib import asynccontextmanager
    import sqlite3
    (tmp_path / "journal").mkdir()
    sqlite3.connect(tmp_path / "journal" / "channel.sqlite3").close()
    cfg = {"_access_config_path":str(tmp_path/"feishu.json"),"state_dir":"journal"}
    tool = "feishu_publish_knowledge"
    job=SimpleNamespace(id=42,task_id=None,status="running",owner_id="owner",assignee_agent_id=4,
                        assignee_turn_id=8,assignment_epoch=1)
    scope = bridge.Grant(job, {"action":"update","contact":"李晗"},cfg,(tool,))
    run = SimpleNamespace(agent_id=4,turn_id=8,ctx=SimpleNamespace(team=SimpleNamespace(team_id=1,feishu_tools=(tool,))))
    @asynccontextmanager
    async def session():
        from sqlalchemy.ext.asyncio import AsyncSession
        db=AsyncMock(spec=AsyncSession)
        db.get.return_value=job
        yield db
    rt = SimpleNamespace(owner_id="owner",session=session)
    monkeypatch.setattr(bridge.service,"team_for",AsyncMock(return_value=SimpleNamespace(id=1)))
    monkeypatch.setattr(bridge,"grant",AsyncMock(return_value=scope))
    record = AsyncMock()
    monkeypatch.setattr(bridge,"record",record)
    handler = AsyncMock(return_value={"url":"https://example.com/docx/example","confirmed":True})
    monkeypatch.setattr(bridge.employee_tools,"execute",handler)
    from app.team import jobs
    events = []
    monkeypatch.setattr(jobs,"event",lambda db,job,stage,data:events.append((stage,data)))
    assert (await bridge.execute(rt,run,tool,{}))["confirmed"]
    assert handler.await_args.args[:3]==(cfg,4,tool)
    assert handler.await_args.kwargs["contact"]=="李晗"
    assert handler.await_args.kwargs["invocation_id"]=="job-42"
    assert events[0][0]=="feishu_delivered"
    assert events[0][1]["result"]=={"url":"https://example.com/docx/example"}
    verified={"document_resolution":"created","parent_verified":True,"body_verified":True,
              "source_messages_verified":3,"same_title_entries":1}
    handler.return_value={"url":"https://example.com/docx/example","confirmed":True,
                          "messages_read":3,"verification":{**verified,"artifact_sha256":"ab"*32,"folder_url":"employee-only location"}}
    await bridge.execute(rt,run,tool,{})
    assert events[-1][1]["result"]=={"url":"https://example.com/docx/example","messages_read":3,
                                     "verification":verified,"artifact_sha256":"ab"*32}
    handler.side_effect=ValueError("授权变更")
    with pytest.raises(bridge.service.TeamError):
        await bridge.execute(rt,run,tool,{})
    assert record.await_args.kwargs=={"category":"validation"}


def test_platform_readback_denial_reports_specific_missing_permission_without_secrets():
    import httpx
    request=httpx.Request("GET","https://open.feishu.cn/open-apis/docx/v1/documents/private-id")
    response=httpx.Response(400,request=request,json={"code":99991679,"msg":"secret token"})
    error=httpx.HTTPStatusError("failure",request=request,response=response)
    code,detail=bridge.platform_failure(error)
    assert code=="feishu_readback_denied" and "docx:document:readonly" in detail
    assert "99991679" in detail and "secret" not in detail and "private-id" not in detail


def test_employee_with_write_only_connection_gets_owner_repair_action(monkeypatch):
    cfg={"roles":{"二狗":{"agent_id":4}}}
    identity={"_employee_oauth":True,"steward":{"app_id":"app"}}
    monkeypatch.setattr(bridge.owner_oauth,"employee_config",lambda *_:identity)
    monkeypatch.setattr(bridge.owner_oauth,"status",lambda *_:{"actions":{"history":True,"doc":False},
                        "scopes":["docx:document:write_only","docx:document:create"]})
    monkeypatch.setattr(bridge.workspace_access,"settings_for",lambda _:{"allow_codex_summary":True,
        "history_sources":{"李晗":{"agent_id":4,"oauth_app":"app"}}})
    proof,tools=bridge.configured(cfg,4,{"action":"update","contact":"李晗"})
    assert "docx:document:readonly" in proof["delivery_blocker"] and "二狗" in proof["delivery_blocker"]
    assert set(tools)=={"feishu_list_sources","feishu_read_history","feishu_read_message","feishu_read_document"}
