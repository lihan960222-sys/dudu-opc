from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import json
import os
import subprocess

import pytest

from app.db.models import Agent, AgentJob
from app.services import agent_store, employee_runtime, feature_tasks, thread_store
from app.services.codex_adapter import feature_team_tools
from app.team import policy, prompt, service, setup, work_verification
from tests.services.thread_harness import owner, sessions  # noqa: F401
from app.team.bridge import TeamBridge


@pytest.fixture
async def staff(sessions, owner, tmp_path):
    async with sessions() as db:
        roles = []
        for name, directory in (("Lead", "desk"), ("Reader", "libraries"), ("Engineer", "custom-project")):
            member = await agent_store.create_agent(db, owner_id=owner, name=name, provider="codex",
                model="gpt-5.5", model_vendor="openai", make_lead=not roles)
            if roles:
                await service.change_member(db, owner, member.id)
            path = tmp_path / directory
            path.mkdir()
            member.role_path = str(path)
            member.workspace_path = str(tmp_path / "ordinary-chat" / str(member.id))
            roles.append(member)
        lead, reader, target = roles
        reader.team_workspaces_readable = True
        reader.delegation_enabled = False
        team = await service.team_for(db, owner)
        await db.commit()
    return team, lead, reader, target


async def test_lead_sees_registered_roots_and_who_can_read_before_dispatch(sessions, owner, staff):
    team, lead, reader, target = staff
    run = SimpleNamespace(agent_id=lead.id, turn_id=1,
                          ctx=SimpleNamespace(team=policy.TurnTeam(team.id, lead.id, True, True)))
    bridge = TeamBridge(SimpleNamespace(session=sessions, owner_id=owner), run)
    try:
        roster = (await bridge.execute("list_agents", {}))["agents"]
        async with sessions() as db:
            catalog = (await setup.catalog(db, owner, lead.id, engines=[]))["agents"]
            _, listing = await prompt.parts(db, team=team, agent=lead, owner_id=owner)
        for result in (roster, catalog):
            by_id = {member["id"]: member for member in result}
            assert [by_id[a.id]["role_path"] for a in (lead, reader, target)] == [
                lead.role_path, reader.role_path, target.role_path]
            assert by_id[reader.id]["read_only_peer_ids"] == [target.id]
            assert by_id[target.id]["read_only_peer_ids"] == []
        assert {a["workspace_path"] for a in catalog} == {lead.role_path, reader.role_path, target.role_path}
        for agent in (lead, reader, target):
            assert agent.name in listing and Path(agent.role_path).name in listing
        assert "ordinary-chat" not in listing
    finally:
        await bridge.close()


async def test_missing_role_directory_is_not_replaced_by_a_numbered_chat_directory(sessions, owner, staff, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "STEWARD_STRICT_OWNER", owner)
    _, lead, _, target = staff
    async with sessions() as db:
        (await db.get(Agent, target.id)).role_path = None
        await db.flush()
        catalog = await setup.catalog(db, owner, lead.id, engines=[])
    row = next(member for member in catalog["agents"] if member["id"] == target.id)
    assert row["role_path"] is None and row["workspace_path"] is None


async def test_read_only_native_turn_discovers_and_inspects_permitted_colleague(
        sessions, owner, staff, tmp_path, monkeypatch):
    team, lead, reader, target = staff
    from app.core.config import settings
    # Restore the headless setting's provenance as well as its value after this turn.
    monkeypatch.setitem(vars(settings), "LOCAL_TOOLS_ENABLED", True)
    monkeypatch.setattr(employee_runtime, "native_enabled", lambda o, who: o == owner and who == reader.id)
    monkeypatch.setenv("SUPARAGENT_EMPLOYEE_RUNTIME_ROOT", str(tmp_path / "control" / "employee-runtime"))
    target_root = Path(target.role_path)
    (target_root / "result.md").write_text("project evidence", encoding="utf-8")
    async with sessions() as db:
        accepted = await feature_tasks.route(db, owner_id=owner, agent_id=reader.id, project="inspection",
            event_key="inspect-colleague", title="Inspect colleague", text="Inspect the engineer's files.")
        task = accepted["task_id"]
        thread, _ = await feature_tasks.session_for(db, owner, task, model=reader.model, workspace_path=reader.role_path)
        dispatched = await feature_tasks.dispatch(db, owner_id=owner, steward_id=lead.id, task_id=task,
            event_key="inspect-colleague", thread_id=thread.id)
        turn = await thread_store.start_turn(db, thread.id, "Inspect", feature_event_key="inspect-colleague")
        job = await db.get(AgentJob, dispatched)
        job.assignee_turn_id, job.status = turn.id, "running"
        project = await feature_tasks.project_runtime_for(db, owner, reader, thread, "plan")
        await db.commit()
    assert project.readable_roots == (target_root,)
    team_ctx = policy.TurnTeam(team.id, reader.id, False, False, strict_hierarchy=True,
                              feature_consult=True, delegation_enabled=False)
    ctx = SimpleNamespace(project_runtime=project, team=team_ctx, approval_mode="plan", aborted=False,
                          community_tools=[], composio_launch=None, composio_exact_tools=None)
    emitted = AsyncMock(return_value=True)
    run = SimpleNamespace(turn_id=turn.id, thread_id=thread.id, agent_id=reader.id, owner_token=turn.owner_token,
                          ctx=ctx, recorder=SimpleNamespace(lost=False, emit=emitted))
    bridge = TeamBridge(SimpleNamespace(session=sessions, owner_id=owner), run)
    args = {"agent_id": target.id, "path": "result.md"}
    try:
        tools = {"list_peer_files", "read_peer_text"}
        assert tools <= set(feature_team_tools(ctx))
        launch = bridge.launch
        list_tools = subprocess.run([launch.command, *launch.args], env={**os.environ, **launch.env},
            input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n",
            text=True, capture_output=True, check=True, timeout=15)
        listed = {t["name"] for t in json.loads(list_tools.stdout)["result"]["tools"]}
        assert listed == set(feature_team_tools(ctx))
        assert {"list_project_files","read_project_text"} <= listed
        assert "replace_project_text" not in listed
        assert all(policy.automatic("mcp__team__" + tool, "plan", team_ctx, args)[0] == "allowed" for tool in tools)
        from tests.services.test_codex_adapter import _turn
        engine = _turn("plan", project_runtime=project, workspace=project.root, feature_workspace=str(project.root),
                       team=team_ctx, team_launch=launch)
        engine._emit = AsyncMock(return_value=True)
        engine.rpc = Mock()
        engine.gate = SimpleNamespace(open=AsyncMock(return_value=None))
        cases = {**dict.fromkeys(tools,args),"list_project_files":{"limit":2},"read_project_text":{"path":"fixture.md"}}
        for request_id, (tool, call_args) in enumerate(cases.items(), start=7):
            await engine._item_started({"id": tool, "type": "mcpToolCall", "server": "team", "tool": tool, "arguments": call_args})
            await engine._server_request({"id": request_id, "method": "mcpServer/elicitation/request", "params": {
                "serverName": "team", "message": f'run tool "{tool}"',
                "_meta": {"codex_approval_kind": "mcp_tool_call", "tool_params": call_args}}})
            assert engine.gate.open.await_args.kwargs["tool"] == "mcp__team__" + tool
        assert not engine.breach and engine.tools and not engine.project.writable
        caps = await bridge.execute("employee_capabilities", {})
        assert caps["read_only_peers"] == [{"id": target.id, "name": target.name, "role_path": target.role_path}]
        assert tools <= set(caps["granted_tools"])
        own_root = Path(reader.role_path)
        (own_root / "fixture.md").write_text("owned evidence", encoding="utf-8")
        from app.services.thread_runtime import ActiveRun, ThreadApprovalGate
        from app.team.bridge import consume_allowed_approval
        active = ActiveRun(thread.id, turn.id, turn.owner_token, ctx, approval_timeout=1)
        runtime = SimpleNamespace(session=sessions,owner_id=owner,_runs={turn.id:active},_waiters={},
                                  _mode_of=lambda _: "plan",hub=SimpleNamespace(notify=lambda _:None))
        gate = ThreadApprovalGate(runtime,active)
        tool = "mcp__team__list_project_files"
        permit = await gate.open(request_id="directory-probe",tool=tool,kind="other",input={},summary="查看自己的目录")
        assert (await gate.decide(permit)).kind == "allow"
        async with sessions() as db:
            assert await consume_allowed_approval(db,turn.id,turn.owner_token,tool,{},return_id=True) == permit.approval_id
            assert not await consume_allowed_approval(db,turn.id,turn.owner_token,tool,{})
        own_entries = await bridge.execute("list_project_files", {}, approval_id=permit.approval_id)
        assert "fixture.md" in {entry["name"] for entry in own_entries["entries"]}
        assert "任务" not in {entry["name"] for entry in own_entries["entries"]}
        own_receipt = emitted.await_args.args[1]["file_receipts"][0]
        assert own_receipt["scope"] == "role" and own_receipt["root"] == str(own_root)
        assert (await bridge.execute("read_project_text", {"path":"fixture.md"}))["text"] == "owned evidence"
        for invalid_args in ({"path":"../other"},{"path":"任务"},{"limit":501},{"agent_id": target.id}):
            with pytest.raises(service.TeamError):
                await bridge.execute("list_project_files", invalid_args)
        entries = await bridge.execute("list_peer_files", {**args, "path": ""})
        assert [entry["name"] for entry in entries["entries"]] == ["result.md"]
        for folder in ("a-docs", "b-tests", "c-source", "d-assets"):
            (target_root / folder).mkdir()
        (target_root / "0-file.txt").touch()
        request = {"agent_id": target.id, "path": "", "limit": 2, "folders_only": True}
        sample = await bridge.execute("list_peer_files", request)
        assert sample == {"path": ".", "entries": [{"name": "a-docs", "folder": True},
                                                   {"name": "b-tests", "folder": True}], "truncated": True}
        listed_receipt = emitted.await_args.args[1]["file_receipts"][0]
        assert listed_receipt["count"] == 2 and listed_receipt["truncated"] is True
        schemas = {tool["name"]: tool["inputSchema"] for tool in json.loads(list_tools.stdout)["result"]["tools"]}
        assert set(schemas["list_peer_files"]["properties"]) == {"agent_id", "path", "limit", "folders_only"}
        assert set(schemas["list_peer_files"]["required"]) == {"agent_id", "path"}
        assert set(schemas["read_peer_text"]["properties"]) == {"agent_id", "path"}
        with pytest.raises(service.TeamError):
            await bridge.execute("read_peer_text", {**args, "limit": 2})
        assert (await bridge.execute("read_peer_text", args))["text"] == "project evidence"
        receipt = emitted.await_args.args[1]["file_receipts"][0]
        assert receipt["root"] == target.role_path and receipt["scope"] != "role"
        async with sessions() as db:
            await thread_store.add_event_in_transaction(db, thread.id, "tool.completed", emitted.await_args.args[1], turn_id=turn.id)
            await db.flush()
            result = await work_verification.assess(db, job, SimpleNamespace(id=turn.id, result="Read result.md"),
                                                    {"kind": "inspection"})
            assert result["state"] == "inspected" and receipt in result["evidence"]
        for invalid in ({**args, "agent_id": lead.id}, {**args, "path": "../outside.txt"}, {**args, "new": "bad"}):
            with pytest.raises(service.TeamError):
                await bridge.execute("read_peer_text", invalid)
        async with sessions() as db:
            (await db.get(Agent, reader.id)).team_workspaces_readable = False
            await db.commit()
        with pytest.raises(service.TeamError):
            await bridge.execute("read_peer_text", args)
        assert (await bridge.execute("employee_capabilities", {}))["read_only_peers"] == []
        assert (target_root / "result.md").read_text(encoding="utf-8") == "project evidence"
        run.owner_token = "expired"
        with pytest.raises(service.TeamError):
            await bridge.execute("list_project_files", {})
    finally:
        await bridge.close()


async def test_native_terminal_receipt_accepts_only_a_readable_registered_peer(sessions, owner, staff):
    _, lead, reader, target = staff
    root, peer = Path(reader.role_path), Path(target.role_path)
    source = peer / "plan.md"
    source.write_text("project notes", encoding="utf-8")
    command = f'cat "{source}"'
    item = {"type": "commandExecution", "id": "check", "status": "completed", "exitCode": 0,
            "command": command, "cwd": str(root),
            "commandActions": [{"type": "read", "command": command, "path": str(source)}]}
    assert "inspection" not in work_verification.command_receipt(item, root)
    receipt = work_verification.command_receipt(item, root, readable_roots=(peer,))
    assert receipt["inspection"] == {"operation": "read", "path": "plan.md", "scope": "peer", "root": str(peer)}
    assert work_verification.command_receipt({**item, "cwd": str(peer)}, root, readable_roots=(peer,))["root"] == str(peer)
    async with sessions() as db:
        thread = await thread_store.create_thread(db, owner_id=owner, agent_id=reader.id, provider="codex")
        turn = await thread_store.start_turn(db, thread.id, "Inspect")
        await thread_store.add_event_in_transaction(db, thread.id, "tool.completed", {"ok": True, "command_receipt": receipt},
                                                   turn_id=turn.id)
        await db.flush()
        run = SimpleNamespace(id=turn.id, result="Read the plan.")
        job = SimpleNamespace(assignee_agent_id=reader.id, owner_id=owner)
        assert (await work_verification.assess(db, job, run, {"kind": "inspection"}))["state"] == "inspected"
        (await db.get(Agent, reader.id)).team_workspaces_readable = False
        await db.flush()
        assert (await work_verification.assess(db, job, run, {"kind": "inspection"}))["state"] == "missing"

async def test_employee_finds_recent_host_logs_through_its_readonly_tool(sessions, owner, staff, tmp_path, monkeypatch):
    from app.core.config import settings
    from app.services import tool_policy
    team, lead, reader, _ = staff
    monkeypatch.setattr(settings, "STEWARD_STRICT_OWNER", owner)
    monkeypatch.setitem(vars(settings), "LOCAL_TOOLS_ENABLED", True)
    vendor = tmp_path / "AppData" / "Vendor"
    logs = vendor / "session" / "logs"
    logs.mkdir(parents=True)
    logfile = logs / "session.log"
    logfile.write_text("private contents", encoding="utf8")
    monkeypatch.setenv("LOCALAPPDATA", str(vendor.parent))
    async with sessions() as db:
        card = await feature_tasks.route(db, owner_id=owner, agent_id=reader.id, project="diagnostics",
            event_key="find-logs", title="Inspect", text="Find the latest log.")
        task = card["task_id"]
        thread, _ = await feature_tasks.session_for(db, owner, task, model=reader.model, workspace_path=reader.role_path)
        dispatched = await feature_tasks.dispatch(db, owner_id=owner, steward_id=lead.id, task_id=task,
            event_key="find-logs", thread_id=thread.id)
        turn = await thread_store.start_turn(db, thread.id, "Find log", feature_event_key="find-logs")
        job = await db.get(AgentJob, dispatched)
        job.assignee_turn_id, job.status = turn.id, "running"
        project = await feature_tasks.project_runtime_for(db, owner, reader, thread, "plan")
        await db.commit()
    ctx = SimpleNamespace(project_runtime=project, approval_mode="plan", aborted=False,
        team=policy.TurnTeam(team.id, reader.id, False, False, strict_hierarchy=True,feature_consult=True))
    run = SimpleNamespace(ctx=ctx, thread_id=thread.id, turn_id=turn.id,owner_token=turn.owner_token,
                          recorder=SimpleNamespace(lost=False,emit=AsyncMock(return_value=True)))
    bridge = TeamBridge(SimpleNamespace(session=sessions, owner_id=owner),run)
    try:
        args = {"operation":"find", "path":str(vendor), "pattern":"*.log", "limit":1}
        assert "inspect_local_path" in feature_team_tools(ctx)
        assert tool_policy.automatic("mcp__team__inspect_local_path","plan",run,args)[0] == "allowed"
        result = await bridge.execute("inspect_local_path", args)
        assert result["scan_complete"] and result["entries"][0]["path"] == str(logfile)
        assert "private contents" not in str(result)
        receipt = run.recorder.emit.await_args.args[1]["file_receipts"][0]
        assert receipt["scope"] == "local" and receipt["operation"] == "list_files" and receipt["count"] == 1
    finally:
        await bridge.close()
