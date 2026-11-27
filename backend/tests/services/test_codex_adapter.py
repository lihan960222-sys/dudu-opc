"""The Codex engine, through a real runtime and a real child process playing
`codex` (tests/services/fake_codex_cli.py)."""

import json
from pathlib import Path
import stat
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import delete

from app.core.config import settings
from app.db.models import Agent, Thread, Turn
from app.services import agent_store, codex_adapter, local_claude_runner
from app.services import thread_store as store
from app.services.codex_adapter import CodexAdapter, codex_policy
from app.services.thread_hub import ThreadHub
from app.services.thread_runtime import ThreadRuntime
from tests.services.thread_harness import (  # noqa: F401
    cleanup,
    events_of,
    finished,
    owner,
    pending_approval,
    reload,
    sessions,
    types_of,
    wait_until,
)

FAKE = Path(__file__).with_name("fake_codex_cli.py")


class FakeCodex:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.plan()

    def plan(self, *steps: dict, **more) -> None:
        (self.folder / "plan.json").write_text(json.dumps({"steps": list(steps) or [{"type": "chat", "text": "hello there"}], **more}))

    def log(self) -> list[dict]:
        path = self.folder / "log.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def received(self, method: str) -> list[dict]:
        return [e["msg"] for e in self.log() if e.get("event") == "in" and e["msg"].get("method") == method]

    def answers(self) -> list:
        return [e["answer"] for e in self.log() if e.get("event") == "approval_answer"]


@pytest.fixture
def codex(monkeypatch, tmp_path):
    folder = tmp_path / "codex"
    folder.mkdir()
    if sys.platform == "win32":
        exe = tmp_path / "codex-bin.cmd"
        exe.write_text(f'@echo off\r\n"{sys.executable}" "{FAKE}" %*\r\n')
    else:
        exe = tmp_path / "codex-bin"
        exe.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FAKE_CODEX_DIR", str(folder))
    monkeypatch.setattr(codex_adapter, "resolve_codex_executable", lambda: exe)
    monkeypatch.setattr(codex_adapter, "resolve_employee_executable", lambda: exe)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setattr(local_claude_runner, "workspace_dir", lambda: workspace)
    fake = FakeCodex(folder)
    fake.workspace = workspace
    return fake


@pytest.fixture
async def crt(sessions, owner, monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_TOOLS_ENABLED", False)
    monkeypatch.setattr(settings, "THREAD_HEARTBEAT_SECONDS", 0.2)
    from app.services import thread_runtime

    monkeypatch.setattr(thread_runtime, "APPROVAL_POLL_SECONDS", 0.1)
    runtime = ThreadRuntime(
        hub=ThreadHub(), adapters={"codex": CodexAdapter()}, status_ttl=0, _session_factory=sessions, owner_id=owner
    )
    yield runtime
    await runtime.shutdown()
    async with sessions() as db:
        await db.execute(delete(Agent).where(Agent.owner_id == owner))
        await db.commit()


async def codex_thread(crt, sessions, owner, mode=None):
    agent_id = None
    if mode is not None:
        async with sessions() as db:
            agent = await agent_store.create_agent(db, owner_id=owner, name=f"cx-{mode}", provider="codex", approval_mode=mode)
        agent_id = agent.id
    return await crt.create_thread(provider="codex", agent_id=agent_id)


def data_of(events, type_):
    return [e.data for e in events if e.type == type_]


def test_policy_mapping():
    assert codex_policy("ask", True) == codex_adapter.CodexPolicy("untrusted", "workspace-write", "workspaceWrite")
    assert codex_policy("accept_edits", True).approval == "untrusted"
    assert codex_policy("plan", True) == codex_adapter.CodexPolicy("never", "read-only", "readOnly")
    assert codex_policy("ask", False).sandbox == "read-only"


def test_employee_only_uses_scoped_tools_when_global_setting_is_implicit():
    from dataclasses import replace
    from app.core import app_settings
    from app.services import feature_files

    project = SimpleNamespace(writable=True)
    frozen = app_settings.capture()
    key = "LOCAL_TOOLS_ENABLED"
    for source, enabled in (("default", True), ("environment", False), ("database", False)):
        frozen[key] = replace(frozen[key], source=source, value=False, effective_value=False)
        with app_settings.use(frozen):
            assert not _turn("ask").tools
            assert _turn("ask", project_runtime=project).tools is enabled
            assert not _turn("plan", project_runtime=project).tools
            features = feature_files.turn_config({}, None, False, project_runtime=project)["features"]
            assert not features["unified_exec"] and not features["shell_tool"]


async def test_service_restarts_resume_private_thread(codex, tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from app.core import host_exec
    from app.services import employee_runtime
    from app.services.provider_adapter import TurnContext
    from tests.services.test_employee_runtime import employee_project

    runtime = employee_project(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "STEWARD_STRICT_OWNER", runtime.owner_id)
    monkeypatch.setattr(settings, "LOCAL_TOOLS_ENABLED", True)
    personal = tmp_path / "personal-codex"
    personal.mkdir()
    (personal / "sessions.index").write_text("personal-session", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(personal))
    monkeypatch.setenv("CODEX_SQLITE_HOME", str(personal))
    # The protocol fixture is a separate CLI process. The real MXC probe has its own test.
    monkeypatch.setattr(employee_runtime.ProjectRuntime, "check_start", lambda self, exe: None)
    monkeypatch.setattr(host_exec, "employee_host_blocker", lambda **_options: None)
    monkeypatch.setattr(host_exec, "employee_process_lifetime", lambda _pid: None)
    environment = employee_runtime.ProjectRuntime.process_environment
    monkeypatch.setattr(employee_runtime.ProjectRuntime, "process_environment",
        lambda self, env=None: {**environment(self, env), "FAKE_CODEX_DIR": str(codex.folder)})
    codex.plan(private_history=True, user_mcp={"personal_files": {}})
    sink = SimpleNamespace(lost=False, emit=AsyncMock(return_value=True), delta=lambda value: None)
    saved = []

    async def bind(state):
        saved.append(state)
        return True

    async def invoke(number, state):
        ctx = TurnContext(thread_id=8, turn_id=number, prompt="continue", workspace=runtime.root,
            model="gpt-fake", provider_session_id="backend-session", provider_state=state,
            resume=number > 1, history=AsyncMock(side_effect=AssertionError("private history is native")),
            feature_workspace=str(runtime.root), approval_mode="accept_edits")
        ctx.project_runtime, ctx.model_vendor, ctx.bind_native_session = runtime, "openai", bind
        return await CodexAdapter().run_turn(ctx, sink, None)

    first = await invoke(1, {"employee_project": runtime.stamp})
    assert first.status == "succeeded", first.error
    state = first.provider_state
    native = state["codex_thread_id"]
    assert saved[0]["codex_thread_id"] == native and state["employee_project"] == runtime.stamp
    assert (runtime.profile / "fixture-threads" / native).is_file()
    for number in (2, 3):
        outcome = await invoke(number, state)
        assert outcome.status == "succeeded", outcome.error
        assert outcome.provider_state["codex_thread_id"] == native
        state = outcome.provider_state
    starts = codex.received("thread/start")
    assert len(starts) == 1 and starts[0]["params"]["ephemeral"] is False
    assert [r["params"]["threadId"] for r in codex.received("thread/resume")] == [native, native]
    for request in [*starts, *codex.received("thread/resume")]:
        params = request["params"]
        assert Path(params["cwd"]) == runtime.root
        assert params["permissions"] == runtime.permission_name
        assert "environments" not in params and "sandbox" not in params
        assert params["config"]["mcp_servers"]["personal_files"]["enabled"] is False
    for request in codex.received("turn/start"):
        params = request["params"]
        assert params["permissions"] == runtime.permission_name
        assert "sandboxPolicy" not in params and "environments" not in params
    assert [p.name for p in personal.iterdir()] == ["sessions.index"]
    assert (personal / "sessions.index").read_text(encoding="utf-8") == "personal-session"
    codex.plan(private_history=True, active_writer=True)
    assert (await invoke(4, state)).error_code == "native_session_busy"
    codex.plan(private_history=True, resume_error="maintenance in progress")
    uncertain = await invoke(5, state)
    assert uncertain.error_code == "native_session_unverified" and uncertain.provider_state == state
    count = len(codex.received("thread/resume"))
    codex.plan(private_history=True, stored_cwd=str(tmp_path / "wrong-project"))
    wrong = await invoke(6, state)
    assert wrong.error_code == "project_binding_changed" and len(codex.received("thread/resume")) == count
    (runtime.profile / "fixture-threads" / native).unlink()
    missing = await invoke(7, state)
    assert missing.error_code == "session_resume_failed" and missing.session_state == "broken"
    assert len(codex.received("thread/start")) == 1 and len(codex.received("turn/start")) == 3


async def test_private_turn_requires_same_scoped_feature_root(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from app.services.provider_adapter import TurnContext
    from tests.services.test_employee_runtime import employee_project

    project = employee_project(tmp_path, monkeypatch)
    monkeypatch.setattr(codex_adapter, "resolve_employee_executable", lambda: tmp_path / "codex.exe")
    ctx = TurnContext(thread_id=8, turn_id=1, prompt="continue", workspace=project.root,
        model="gpt-fake", provider_session_id="backend", resume=False, history=AsyncMock(),
        provider_state={"employee_project": project.stamp}, project_runtime=project, model_vendor="openai",
        bind_native_session=AsyncMock(), feature_workspace=None)
    for workspace in (None, str(tmp_path / "other")):
        ctx.feature_workspace = workspace
        outcome = await CodexAdapter().run_turn(ctx, None, None)
        assert outcome.error_code == "project_binding_changed"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows employee launch policy")
async def test_bound_private_session_restarts_in_process(tmp_path, monkeypatch):
    from dataclasses import replace
    from unittest.mock import AsyncMock, Mock
    from uuid import uuid4
    from app.core import app_settings, host_exec
    from app.services import employee_runtime
    from app.services.provider_adapter import TurnContext
    from tests.services.test_employee_runtime import employee_project

    # Exercise the same named native profile and reviewer as a home workstation.
    monkeypatch.setattr(settings, "STEWARD_STRICT_OWNER", "employee-test-owner")
    project = employee_project(tmp_path, monkeypatch)
    monkeypatch.setattr(host_exec, "noninteractive_window_station", lambda: False)
    monkeypatch.setattr(host_exec, "windows_worker_token_status", lambda: "standard")
    snapshot = app_settings.capture()
    key = "LOCAL_TOOLS_ENABLED"
    snapshot[key] = replace(snapshot[key], value=False, effective_value=False, source="default")
    probe = AsyncMock(side_effect=AssertionError("a regular assignment must never launch external checks"))
    monkeypatch.setattr(employee_runtime.cli_probe, "run", probe)
    exe = tmp_path / "codex.exe"
    exe.write_bytes(b"unused protocol fixture")
    monkeypatch.setattr(codex_adapter, "resolve_employee_executable", lambda: exe)
    project.process_environment()
    (project.profile / "auth.json").write_text("{}", encoding="utf-8")
    assert (await codex_adapter.employee_codex_status(project, "openai")).ready
    native = str(uuid4())
    rollout = project.profile / "sessions" / native
    stored_thread = {"id": native, "cwd": str(project.root), "path": str(rollout), "modelProvider": "openai",
                     "ephemeral": False, "status": {"type": "idle"}}
    requests = []
    workspace_roots = [str(project.root)]
    attached_tools = {}
    local_environment = {"environmentId": "local", "cwd": str(project.root), "runtimeWorkspaceRoots": [str(project.root)]}
    resumed_environments = [local_environment]
    effective_environments = []
    ignore_clear = False
    async def rpc(method, params, **_options):
        requests.append((method, params))
        if method in {"thread/start", "thread/resume"}:
            attached_tools.clear()
            attached_tools.update(params["config"]["mcp_servers"])
            # Resume does not accept the start-only environments field. Supplying cwd selects the local default.
            effective_environments[:] = resumed_environments if method == "thread/resume" else params.get("environments", [local_environment])
            return {"thread": {**stored_thread, "environments": list(effective_environments)}, "modelProvider": "openai", "model": params["model"],
                "approvalPolicy": params["approvalPolicy"], "approvalsReviewer": params["approvalsReviewer"],
                "activePermissionProfile": {"id": params["permissions"]},
                "cwd": str(project.root), "runtimeWorkspaceRoots": list(workspace_roots),
                "instructionSources": [str(project.root / "AGENTS.md")]}
        if method == "thread/settings/update":
            assert params == {"threadId": native, "environments": []}
            if not ignore_clear:
                effective_environments.clear()
            return {}
        if method == "thread/read":
            return {"thread": {**stored_thread, "environments": list(effective_environments)}}
        return {"initialize": {}, "account/read": {"account": {"type": "chatgpt"}},
                "config/read": {"config": {}}, "thread/turns/list": {"data": []},
                "skills/list": {"data": [{"cwd": str(project.root), "skills": []}]},
                "mcpServerStatus/list": {"data": [{"name": server, "tools": {tool:{"name":tool} for tool in conf["enabled_tools"]}}
                    for server,conf in attached_tools.items()], "nextCursor": None},
                "turn/start": {"turn": {"id": "turn-1"}}}[method]

    async def done(run):
        run.completed, run.turn_status, run.final_text = True, "completed", "done"
    virtual_rpc = Mock(request=rpc, close=AsyncMock(), over_limit=False)
    spawn = AsyncMock(return_value=virtual_rpc)
    monkeypatch.setattr(codex_adapter.StdioRpc, "spawn", spawn)
    monkeypatch.setattr(codex_adapter._CodexTurn, "_pump", done)

    async def start(state, number, *, cloud=False, readonly=False):
        requested_project = replace(project, writable=False) if readonly else project
        ctx = TurnContext(thread_id=12, turn_id=number, prompt="continue", workspace=project.root, model="gpt-fake",
            provider_session_id="backend", provider_state=state, resume=number > 1, history=AsyncMock(),
            feature_workspace=str(project.root), project_runtime=requested_project, model_vendor="openai",
            approval_mode="plan" if readonly else "accept_edits", bind_native_session=AsyncMock(return_value=True))
        if cloud:
            ctx.team = SimpleNamespace(is_lead=False, strict_hierarchy=True, delegation_enabled=False,
                feishu_tools=("feishu_list_sources",))
            ctx.team_launch = SimpleNamespace(command="synthetic-mcp", args=(), env={})
        with app_settings.use(snapshot):
            return await CodexAdapter().run_turn(ctx, SimpleNamespace(lost=False, emit=AsyncMock(return_value=True)), None)

    state = {"employee_project": project.stamp}
    for number in (1, 2, 3):
        result = await start(state, number)
        assert result.status == "succeeded", result.error
        state = result.provider_state
        assert state["codex_thread_id"] == native
    assert sum(m == "thread/start" for m, _ in requests) == 1
    assert [p["threadId"] for m, p in requests if m == "thread/resume"] == [native, native]
    assert all((p["approvalPolicy"], p["approvalsReviewer"]) == ("on-request", "auto_review")
               for m, p in requests if m in {"thread/start", "thread/resume", "turn/start"})
    assert all(p["permissions"] == project.permission_name for m, p in requests if m == "turn/start")
    forged = {**state, "codex_thread_id": str(uuid4())}
    count = sum(m == "thread/read" for m, _ in requests)
    outcome = await start(forged, 4)
    assert outcome.error_code == "project_binding_changed" and sum(m == "thread/read" for m, _ in requests) == count
    workspace_roots.append(str(tmp_path / "another employee"))
    unexpected = await start(state, 5)
    assert unexpected.error_code == "codex_policy_mismatch" and sum(m == "turn/start" for m, _ in requests) == 3
    assert spawn.await_count == 5
    monkeypatch.setattr(host_exec, "windows_worker_token_status", lambda: "administrator")
    rejected = await start(state, 6)
    assert rejected.error_code == "employee_boundary_unavailable" and spawn.await_count == 5
    monkeypatch.setattr(host_exec, "windows_worker_token_status", lambda: "standard")
    for source in ("environment", "database"):
        snapshot[key] = replace(snapshot[key], source=source)
        assert (await start(state, 7)).error_code == "local_tools_disabled" and spawn.await_count == 5
    workspace_roots[:] = [str(project.root)]
    cloud = await start(state, 8, cloud=True)
    assert cloud.status == "succeeded", cloud.error
    thread_config = next(p for m,p in reversed(requests) if m == "thread/resume")
    assert "environments" not in thread_config and not thread_config["config"]["features"]["shell_tool"]
    assert "feishu_list_sources" in thread_config["config"]["mcp_servers"]["team"]["enabled_tools"]
    assert next(p for m,p in reversed(requests) if m == "turn/start")["environments"] == []
    calls = [m for m, _ in requests]
    update_at = calls.index("thread/settings/update")
    assert calls[update_at + 1] == "thread/read" and calls[-1] == "turn/start"
    delivered = calls.count("turn/start")
    ignore_clear = True
    refused = await start(state, 9, cloud=True)
    assert refused.error_code == "codex_policy_mismatch" and [m for m, _ in requests].count("turn/start") == delivered
    clears = [m for m, _ in requests].count("thread/settings/update")
    resumed_environments[:] = [{**local_environment, "cwd": str(tmp_path / "other employee")}]
    wrong = await start(state, 10, cloud=True)
    assert wrong.error_code == "codex_policy_mismatch"
    assert [m for m, _ in requests].count("thread/settings/update") == clears and [m for m, _ in requests].count("turn/start") == delivered
    resumed_environments[:] = [local_environment]
    ignore_clear = False
    plan = await start(state, 11, readonly=True)
    assert plan.status == "succeeded", plan.error
    turn = next(p for m, p in reversed(requests) if m == "turn/start")
    assert turn["environments"] == [] and turn["permissions"] == replace(project, writable=False).permission_name
    snapshot[key] = replace(snapshot[key], source="default")
    enabled_plan = await start(state, 12, readonly=True)
    assert enabled_plan.status == "succeeded", enabled_plan.error
    readonly_config = next(p for m, p in reversed(requests) if m == "thread/resume")
    assert readonly_config["config"]["features"]["shell_tool"]
    readonly_turn = next(p for m, p in reversed(requests) if m == "turn/start")
    assert readonly_turn.get("environments") != []
    assert readonly_turn["permissions"] == replace(project, writable=False).permission_name
    probe.assert_not_awaited()


def test_scheduled_knowledge_allows_only_scoped_feishu_tools():
    from app.services import feature_files

    selected = ("feishu_doc_read", "feishu_doc_write")
    team = SimpleNamespace(feishu_tools=selected, delegation_enabled=True)
    ctx = SimpleNamespace(team=team, scheduled_knowledge=True)
    assert codex_adapter.feature_team_tools(ctx) == selected
    config = feature_files.turn_config({"mcp_servers": {"personal": {}}, "plugins": {"own": {}}},
        None, False, project_runtime=object(), scheduled_knowledge=True)
    assert config["mcp_servers"] == {"personal": {"enabled": False}}
    assert config["plugins"] == {"own": {"enabled": False}}
    assert not any(config["features"].values()) and config["project_doc_max_bytes"] == 0


def test_turn_permission_requests_follow_native_workstation_edit_grant():
    from app.services.feature_files import turn_config

    def enabled(mode="accept_edits", *, workstation=True, writable=True, tools=True, scheduled=False):
        project = SimpleNamespace(local_workstation=workstation, writable=writable)
        config = turn_config({}, None, tools, project_runtime=project, scheduled_knowledge=scheduled,
                             approval_mode=mode)
        features = config["features"]
        assert features["exec_permission_approvals"] is features["request_permissions_tool"]
        return features["request_permissions_tool"]

    assert enabled()
    assert not enabled("ask") and not enabled("plan")
    assert not enabled(workstation=False) and not enabled(writable=False)
    assert not enabled(tools=False) and not enabled(scheduled=True)


def test_private_sqlite_start_error_explains_path_and_permissions(tmp_path, monkeypatch):
    from tests.services.test_employee_runtime import employee_project

    project = employee_project(tmp_path, monkeypatch)
    run = _turn("plan", project_runtime=project, workspace=project.root, feature_workspace=str(project.root))
    result = run._failed("Codex state sqlite migration 23: (code: 14) unable to open database file", "cli_error")
    assert result.error_code == "employee_session_storage_unavailable"
    assert "路径长度" in result.error and "写入权限" in result.error
    assert run._failed("Provider temporarily offline", "cli_error").error_code == "cli_error"


def test_disabled_skill_launch_rule_preserves_private_rules_without_writing(tmp_path, monkeypatch):
    import tomllib
    from tests.services.test_employee_runtime import employee_project

    project = employee_project(tmp_path, monkeypatch)
    project.process_environment()
    config = project.profile / "config.toml"
    content = '[[skills.config]]\nname="other"\nenabled=false\n'
    config.write_text(content, encoding="utf-8")
    disabled = project.root / ".agents" / "skills" / "example" / "SKILL.md"
    args = codex_adapter._private_skill_options(project, {disabled})
    assert args[0] == "-c" and tomllib.loads(args[1])["skills"]["config"] == [
        {"name": "other", "enabled": False}, {"path": str(disabled), "enabled": False}]
    assert config.read_text(encoding="utf-8") == content
    assert codex_adapter._private_skill_options(project, set()) == []


async def test_native_tool_discovery_checks_real_reported_names_and_skill_scope(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from tests.services.test_employee_runtime import employee_project

    project = employee_project(tmp_path, monkeypatch)
    skill = project.root / ".agents" / "skills" / "diagnose" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# Diagnose", encoding="utf-8")
    run = _turn("plan", project_runtime=project, workspace=project.root, feature_workspace=str(project.root))
    run.thread_id = "private-thread"
    entry = {"enabled": True, "name": "diagnose", "path": str(skill), "scope": "repo"}
    skills = {"data": [{"cwd": str(project.root), "skills": [entry], "errors": []}]}
    row = {"name": "team", "tools": {"feishu_read_document": {"name": "feishu_read_document"}}}
    servers = {"data": [row], "nextCursor": None}
    config = {"mcp_servers": {"unused-personal": {"enabled": False}, "team": {
        "enabled": True, "enabled_tools": ["feishu_read_document"]}}}
    run._call = AsyncMock(side_effect=[skills, servers])
    seen = await run._project_discovery(config)
    assert seen["project_skills"] == ["diagnose"] and seen["mcp"] == {"team": ["feishu_read_document"]}
    assert run._call.await_args_list[1].args[1]["threadId"] == "private-thread"
    run.disabled_project_skills = {skill}
    run._call = AsyncMock(side_effect=[skills])
    with pytest.raises(ValueError, match="disabled"):
        await run._project_discovery(config)
    run.disabled_project_skills = set()
    row["tools"]["unexpected"] = {"name": "unexpected"}
    run._call = AsyncMock(side_effect=[skills, servers])
    with pytest.raises(ValueError):
        await run._project_discovery(config)
    entry["path"] = str(tmp_path / "other-employee" / "SKILL.md")
    run._call = AsyncMock(side_effect=[skills])
    with pytest.raises(ValueError):
        await run._project_discovery(config)


def _turn(mode, **launches):
    fields = dict(approval_mode=mode, computer=None, team_launch=None, composio_launch=None, browser=None,
                  community_launch=None, workspace=None)
    ctx = SimpleNamespace(**{**fields, **launches})
    return codex_adapter._CodexTurn(ctx=ctx, sink=None, gate=None, exe=None, codex_thread=None)


@pytest.mark.parametrize(("method", "params", "tool"), [
    ("item/commandExecution/requestApproval", {"command": "verify.cmd", "cwd": "C:/project",
                                              "availableDecisions": ["accept", "cancel"]}, "Bash"),
    ("item/fileChange/requestApproval", {"grantRoot": "C:/output"}, "apply_patch"),
    ("item/permissions/requestApproval", {"permissions": {"network": {"enabled": True}}},
     "codex.request_permissions"),
])
@pytest.mark.parametrize("answer", ["allow", "deny", "resolved", "item_finished"])
async def test_employee_native_approval_resumes_only_live_request(tmp_path, monkeypatch, method, params, tool, answer):
    import asyncio
    from unittest.mock import AsyncMock, Mock

    monkeypatch.setattr(settings, "LOCAL_TOOLS_ENABLED", True)
    project = SimpleNamespace(writable=True, root=tmp_path)
    turn = _turn("accept_edits", project_runtime=project, feature_workspace=str(tmp_path))
    turn.rpc = Mock()
    decision = asyncio.get_running_loop().create_future()

    async def wait_for_decision(_handle):
        return await decision

    turn.gate = SimpleNamespace(open=AsyncMock(return_value=SimpleNamespace(tool="")),
                                decide=wait_for_decision, cancel=AsyncMock())
    await turn._server_request({"id": 47, "method": method, "params": {**params, "itemId":"native-item"}})
    assert turn.gate.open.await_args.kwargs["tool"] == tool
    turn.rpc.respond.assert_not_called()
    if answer == "resolved":
        await turn._notification({"method": "serverRequest/resolved", "params": {"requestId": 47}})
        turn.gate.cancel.assert_awaited_once_with("codex-47")
    if answer == "item_finished":
        await turn._item_completed({"id":"native-item", "type":"commandExecution", "status":"failed"})
        turn.gate.cancel.assert_awaited_once_with("codex-47")
    decision.set_result(SimpleNamespace(kind=codex_adapter.ALLOW if answer == "allow" else "deny"))
    await asyncio.gather(*turn.answering)
    if answer in ("resolved", "item_finished"):
        turn.rpc.respond.assert_not_called()
    elif tool == "codex.request_permissions":
        turn.rpc.respond.assert_called_once_with(47, {"scope": "turn",
            "permissions": params["permissions"] if answer == "allow" else {}})
    else:
        denied = "cancel" if tool == "Bash" else "decline"
        turn.rpc.respond.assert_called_once_with(47, {"decision": "accept" if answer == "allow" else denied})


async def test_retired_native_request_cannot_answer_reused_rpc_id(tmp_path, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock, Mock
    monkeypatch.setattr(settings, "LOCAL_TOOLS_ENABLED", True)
    turn = _turn("accept_edits", project_runtime=SimpleNamespace(writable=True, root=tmp_path),
                 feature_workspace=str(tmp_path))
    turn.rpc = Mock()
    waiters = {}
    async def opened(request_id, **kwargs):
        waiters[request_id] = asyncio.get_running_loop().create_future()
        return SimpleNamespace(request_id=request_id)
    async def decide(handle):
        return await waiters[handle.request_id]
    turn.gate = SimpleNamespace(open=opened, decide=decide, cancel=AsyncMock())
    message = {"id":42, "method":"item/commandExecution/requestApproval",
               "params":{"command":"example", "cwd":str(tmp_path)}}
    await turn._server_request(message)
    await turn._notification({"method":"serverRequest/resolved", "params":{"requestId":42}})
    await turn._server_request(message)
    first, second = waiters.values()
    first.set_result(SimpleNamespace(kind="allow"))
    await asyncio.sleep(0)
    turn.rpc.respond.assert_not_called()
    second.set_result(SimpleNamespace(kind="deny"))
    await asyncio.gather(*turn.answering)
    turn.rpc.respond.assert_called_once_with(42, {"decision":"decline"})


def test_approve_for_me_keeps_attached_apps_on_the_app_gate():
    # Codex's own reviewer would approve these calls itself and the app gate would refuse them (not_approved).
    plain = _turn("approve_for_me")
    assert plain.mode == "approve_for_me"
    for launch in ("composio_launch", "team_launch", "browser", "community_launch"):
        turn = _turn("approve_for_me", **{launch: SimpleNamespace()})
        assert turn.mode == "ask" and turn.policy.reviewer == "user", launch


def test_native_workstation_routing_is_independent_of_attached_tools(monkeypatch):
    monkeypatch.setattr(codex_adapter, "_project_tools_allowed", lambda: True)
    project = SimpleNamespace(writable=True, local_workstation=True)
    for mode in ("accept_edits", "approve_for_me", "full_access"):
        run = _turn(mode, project_runtime=project, team_launch=object(), community_launch=object())
        assert (run.mode, run.policy.approval, run.policy.reviewer) == (mode, "on-request", "auto_review")
    assert _turn("ask", project_runtime=project, team_launch=object()).policy.reviewer == "user"
    project.writable = False
    assert _turn("plan", project_runtime=project, team_launch=object()).policy.reviewer == "user"
    monkeypatch.setattr(codex_adapter, "_project_tools_allowed", lambda: False)
    project.writable = True
    assert _turn("accept_edits", project_runtime=project, team_launch=object()).policy.reviewer == "user"


async def test_project_app_permission_needs_live_matching_call_and_does_not_replay():
    import asyncio
    from unittest.mock import AsyncMock
    from app.services.project_mcp_approvals import ProjectMcpApprovals
    from app.services.provider_adapter import ApprovalHandle, Decision, ALLOW
    from app.services.turn_clock import TurnClock

    gate = AsyncMock()
    gate.open.return_value = ApprovalHandle("receipt", "request")
    gate.decide.return_value = Decision(ALLOW)
    permissions = ProjectMcpApprovals(gate, TurnClock(10))
    tool = "mcp__team__feishu_create_document"
    args = {"title": "Meeting", "body": ""}
    # The shim and event stream can arrive in either order. Altered arguments,
    # a second identical request, and late requests never get this receipt.
    altered = asyncio.create_task(permissions.claim(tool, {**args, "body": "different"}))
    original = asyncio.create_task(permissions.claim(tool, args))
    await asyncio.sleep(0)
    permissions.started("item-1", tool, args, "Create a document")
    assert await original == "receipt"
    gate.native_response.assert_awaited_once_with("receipt", True)
    duplicate = asyncio.create_task(permissions.claim(tool, args))
    await asyncio.sleep(0)
    await permissions.finished("item-1")
    await permissions.close()
    assert await altered is None and await duplicate is None
    assert await permissions.claim(tool, args) is None
    gate.open.assert_awaited_once()
    assert gate.open.await_args.kwargs["input"] == args
    gate.native_response.assert_awaited_with("receipt", False)


async def test_local_employee_cannot_approve_another_environments_permissions(tmp_path):
    from unittest.mock import AsyncMock, Mock

    run = _turn("accept_edits", project_runtime=SimpleNamespace(root=tmp_path, writable=True))
    run.rpc, run.gate = Mock(), AsyncMock()
    await run._server_request({"id": 1, "method": "item/permissions/requestApproval",
                               "params": {"environmentId": "remote", "permissions": {"network": {"enabled": True}}}})
    run.rpc.respond.assert_called_once_with(1, {"permissions": {}, "scope": "turn"})
    run.gate.open.assert_not_awaited()


@pytest.mark.parametrize(("kind", "hint", "written", "request_kind", "expected"), [
    ("deny", True, True, "command", True),
    ("deny", False, True, "command", False),
    ("deny", True, False, "command", False),
    ("expired", True, True, "command", False),
    ("deny", True, True, "humanCommand", False),
])
async def test_steward_can_suggest_a_narrower_request_only_after_a_real_decline(
        tmp_path, kind, hint, written, request_kind, expected):
    from unittest.mock import AsyncMock, Mock
    from app.services.employee_native_review import NARROW_REQUEST_HINT
    from app.services.provider_adapter import ApprovalHandle, Decision

    run = _turn("accept_edits", project_runtime=SimpleNamespace(root=tmp_path, writable=True), turn_id=1)
    run.rpc = Mock()
    run.rpc.respond.return_value = written
    run.steer = AsyncMock(return_value="steered")
    run.gate = SimpleNamespace(decide=AsyncMock(return_value=Decision(kind, NARROW_REQUEST_HINT if hint else None)))
    run.requests[1] = "live"
    run.permit_inputs[1] = {"kind": request_kind}
    await run._answer(ApprovalHandle("permit", "live", "Bash"), 1, "live",
                      lambda allow: {"decision": "accept" if allow else "decline"})
    run.rpc.respond.assert_called_once_with(1, {"decision": "decline"})
    if expected:
        run.steer.assert_awaited_once_with(NARROW_REQUEST_HINT)
    else:
        run.steer.assert_not_awaited()


@pytest.mark.parametrize("secret", [False, True])
async def test_experimental_user_question_uses_existing_job_clarification(secret):
    from unittest.mock import Mock

    run = _turn("ask", project_runtime=SimpleNamespace(writable=True))
    run.rpc = Mock()
    await run._server_request({"id":51, "method":"item/tool/requestUserInput", "params":{
        "questions":[{"id":"scope", "question":"Which scope?", "isSecret":secret}]}})
    run.rpc.respond.assert_called_once()
    args, kwargs = run.rpc.respond.call_args
    assert args == (51,) and kwargs["error"]["code"] == -32601
    assert ("credentials" if secret else "NEEDS INPUT:") in kwargs["error"]["message"]
    assert not run.requests


async def test_codex_waits_for_the_exact_routine_connected_app_server(crt, sessions, codex, monkeypatch):
    thread = await crt.create_thread(provider="codex")
    adapter = crt.adapters["codex"]
    run_turn = adapter.run_turn

    async def with_routine_grant(ctx, sink, gate):
        ctx.composio_launch = SimpleNamespace(command="composio-mcp", args=[], env={})
        ctx.composio_apps = {"gmail": "read"}
        ctx.composio_exact_tools = ["gmail_fetch_emails"]
        return await run_turn(ctx, sink, gate)

    monkeypatch.setattr(adapter, "run_turn", with_routine_grant)
    await crt.send_turn(thread.id, "Search authorized Gmail.")
    await finished(sessions, thread.id)

    started = codex.received("thread/start")[0]["params"]
    server = started["config"]["mcp_servers"]["suparagent_composio"]
    assert server["enabled_tools"] == ["gmail_fetch_emails"]
    assert server["required"] is True and server["startup_timeout_sec"] == 10
    assert server["default_tools_approval_mode"] == "prompt"
    assert "exactly these tools: gmail_fetch_emails" in started["developerInstructions"]
    assert "call the matching tool before concluding that the app is unavailable" in started["developerInstructions"]


async def test_a_chat_turn_then_a_resumed_one(crt, sessions, owner, codex):
    thread = await codex_thread(crt, sessions, owner)
    assert thread.session_state == "new"
    await crt.send_turn(thread.id, "hi")
    await finished(sessions, thread.id)
    events = await events_of(sessions, thread.id)
    assert data_of(events, "assistant.message") == [{"text": "hello there"}]
    timings, = data_of(events, "engine.timing")
    stages = ("process_ms", "initialize_ms", "thread_ready_ms", "turn_start_ms", "first_output_ms", "finished_ms")
    assert set(timings) == set(stages)
    assert [timings[k] for k in stages] == sorted(timings.values())
    assert data_of(events, "turn.completed")[0]["result"] == "hello there"
    row = await reload(sessions, Thread, thread.id)
    assert row.session_state == "active"
    codex_id = row.provider_state["codex_thread_id"]
    started = codex.received("thread/start")[0]["params"]
    # Tools off: read-only, nothing asked.
    assert (started["approvalPolicy"], started["sandbox"]) == ("never", "read-only")
    assert started["cwd"] == str(codex.workspace.resolve()) or started["cwd"] == str(codex.workspace)
    turn = codex.received("turn/start")[0]["params"]
    assert turn["sandboxPolicy"] == {"type": "readOnly"}
    assert turn["input"] == [{"type": "text", "text": "hi"}]

    codex.plan({"type": "chat", "text": "again"})
    await crt.send_turn(thread.id, "more")
    await finished(sessions, thread.id, count=2)
    resumed = codex.received("thread/resume")
    assert [r["params"]["threadId"] for r in resumed] == [codex_id]
    turn = await reload(sessions, Turn, (await crt_turns(sessions, thread.id))[-1])
    assert turn.status == "succeeded" and turn.usage == {"input_tokens": 11, "output_tokens": 7, "cached_input_tokens": 3}


async def crt_turns(sessions, thread_id):
    async with sessions() as db:
        return [t.id for t in await store.list_turns(db, thread_id)]


async def test_a_command_waits_for_allow(crt, sessions, owner, codex, monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_TOOLS_ENABLED", True)
    codex.plan({"type": "command", "command": "echo hi > out.txt"})
    thread = await codex_thread(crt, sessions, owner, mode="ask")
    await crt.send_turn(thread.id, "make a file")
    approval = await pending_approval(sessions, thread.id)
    assert approval.tool == "Bash"
    assert approval.input["command"] == "/bin/zsh -lc 'echo hi > out.txt'"
    assert approval.summary == "echo hi > out.txt"
    assert approval.kind == "command"
    started = codex.received("thread/start")[0]["params"]
    assert (started["approvalPolicy"], started["sandbox"]) == ("untrusted", "workspace-write")
    assert await crt.answer_approval(approval.id, "allow")
    await finished(sessions, thread.id)
    assert codex.answers()[0]["result"] == {"decision": "accept"}
    events = await events_of(sessions, thread.id)
    tool = data_of(events, "tool.started")[0]
    assert tool["name"] == "Bash" and tool["summary"] == "echo hi > out.txt"
    assert data_of(events, "tool.completed")[0]["ok"] is True
    assert (Path(thread.workspace_path) / "out.txt").read_text() == "hi\n"
    assert data_of(events, "turn.completed")[0]["result"] == "done"


async def test_a_denied_command_is_declined(crt, sessions, owner, codex, monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_TOOLS_ENABLED", True)
    codex.plan({"type": "command"})
    thread = await codex_thread(crt, sessions, owner, mode="ask")
    await crt.send_turn(thread.id, "make a file")
    approval = await pending_approval(sessions, thread.id)
    assert await crt.answer_approval(approval.id, "deny")
    await finished(sessions, thread.id)
    assert codex.answers()[0]["result"] == {"decision": "decline"}
    events = await events_of(sessions, thread.id)
    assert data_of(events, "tool.completed")[0]["ok"] is False
    assert not (Path(thread.workspace_path) / "out.txt").exists()


async def test_accept_edits_lets_a_workspace_edit_through_without_a_card(crt, sessions, owner, codex, monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_TOOLS_ENABLED", True)
    codex.plan({"type": "file"})  # a file in the folder the turn runs in
    thread = await codex_thread(crt, sessions, owner, mode="accept_edits")
    await crt.send_turn(thread.id, "write notes")
    await finished(sessions, thread.id)
    assert codex.answers()[0]["result"] == {"decision": "accept"}
    async with sessions() as db:
        assert await store.list_approvals(db, thread.id) == []


async def test_accept_edits_still_asks_about_an_edit_outside_the_workspace(crt, sessions, owner, codex, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "LOCAL_TOOLS_ENABLED", True)
    codex.plan({"type": "file", "path": str(tmp_path / "elsewhere.txt")})
    thread = await codex_thread(crt, sessions, owner, mode="accept_edits")
    await crt.send_turn(thread.id, "write notes")
    approval = await pending_approval(sessions, thread.id)
    assert approval.tool == "apply_patch" and approval.kind == "file_write"
    assert approval.input["changes"][0]["path"] == str(tmp_path / "elsewhere.txt")
    assert await crt.answer_approval(approval.id, "deny")
    await finished(sessions, thread.id)
    assert codex.answers()[0]["result"] == {"decision": "decline"}


async def test_plan_mode_is_read_only_and_declines_without_a_card(crt, sessions, owner, codex, monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_TOOLS_ENABLED", True)
    codex.plan({"type": "command"})
    thread = await codex_thread(crt, sessions, owner, mode="plan")
    await crt.send_turn(thread.id, "go")
    await finished(sessions, thread.id)
    started = codex.received("thread/start")[0]["params"]
    assert (started["approvalPolicy"], started["sandbox"]) == ("never", "read-only")
    assert codex.answers()[0]["result"] == {"decision": "decline"}
    async with sessions() as db:
        assert await store.list_approvals(db, thread.id) == []


async def test_an_mcp_approval_goes_through_the_gate(crt, sessions, owner, codex, monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_TOOLS_ENABLED", True)
    codex.plan({"type": "mcp"})
    thread = await codex_thread(crt, sessions, owner, mode="ask")
    await crt.send_turn(thread.id, "search")
    approval = await pending_approval(sessions, thread.id)
    assert approval.tool == "mcp:docs"
    assert await crt.answer_approval(approval.id, "deny")
    await finished(sessions, thread.id)
    assert codex.answers()[0]["result"] == {"action": "decline"}


async def test_an_unknown_request_is_refused(crt, sessions, owner, codex, monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_TOOLS_ENABLED", True)
    codex.plan({"type": "unknown_request"})
    thread = await codex_thread(crt, sessions, owner, mode="ask")
    await crt.send_turn(thread.id, "go")
    await finished(sessions, thread.id)
    assert codex.answers()[0]["error"]["code"] == -32601


async def test_a_lost_codex_thread_breaks_the_session(crt, sessions, owner, codex):
    thread = await codex_thread(crt, sessions, owner)
    async with sessions() as db:
        await store.set_session_state(db, thread.id, "active")
        row = await db.get(Thread, thread.id)
        row.provider_state = {"codex_thread_id": "gone"}
        await db.commit()
    await crt.send_turn(thread.id, "hi")
    await finished(sessions, thread.id)
    failed = data_of(await events_of(sessions, thread.id), "turn.failed")[0]
    assert failed["error_code"] == "session_resume_failed"
    assert (await reload(sessions, Thread, thread.id)).session_state == "broken"
    assert codex.received("turn/start") == []
    # A reset clears the old Codex thread, and the next turn starts a new one.
    await crt.reset_session(thread.id)
    assert (await reload(sessions, Thread, thread.id)).provider_state is None


async def test_active_without_a_codex_thread_is_broken_not_fresh(crt, sessions, owner, codex):
    thread = await codex_thread(crt, sessions, owner)
    async with sessions() as db:
        await store.set_session_state(db, thread.id, "active")
    await crt.send_turn(thread.id, "hi")
    await finished(sessions, thread.id)
    assert data_of(await events_of(sessions, thread.id), "turn.failed")[0]["error_code"] == "session_resume_failed"
    assert codex.received("thread/start") == []


async def test_a_policy_codex_did_not_apply_fails_closed(crt, sessions, owner, codex):
    codex.plan({"type": "chat"}, policy_mismatch=True)
    thread = await codex_thread(crt, sessions, owner)
    await crt.send_turn(thread.id, "hi")
    await finished(sessions, thread.id)
    assert data_of(await events_of(sessions, thread.id), "turn.failed")[0]["error_code"] == "codex_policy_mismatch"
    assert codex.received("turn/start") == []


async def test_stop_interrupts_and_keeps_the_partial_text(crt, sessions, owner, codex):
    codex.plan({"type": "slow"})
    thread = await codex_thread(crt, sessions, owner)
    await crt.send_turn(thread.id, "long one")

    async def typing():
        return bool(codex.received("turn/start"))

    await wait_until(typing)
    import asyncio

    await asyncio.sleep(0.5)
    await crt.stop_turn(thread.id)
    await finished(sessions, thread.id)
    events = await events_of(sessions, thread.id)
    assert data_of(events, "turn.cancelled")[0]["error_code"] == "stopped"
    assert {"text": "partial words", "incomplete": True} in data_of(events, "assistant.message")
    assert codex.received("turn/interrupt")
    # The session survives a stop.
    row = await reload(sessions, Thread, thread.id)
    assert row.session_state == "active" and row.provider_state["codex_thread_id"]


async def test_a_failed_turn_reports_codex_error(crt, sessions, owner, codex):
    codex.plan({"type": "fail", "message": "usage limit reached"})
    thread = await codex_thread(crt, sessions, owner)
    await crt.send_turn(thread.id, "hi")
    await finished(sessions, thread.id)
    failed = data_of(await events_of(sessions, thread.id), "turn.failed")[0]
    # A limit the engine reports is classified for every engine (engine_errors.py).
    assert failed["error_code"] == "engine_limited"
    assert failed["error"].startswith("Codex has reached its usage limit (Codex says: \"usage limit reached\")")


async def test_a_crash_mid_turn_fails_with_the_cli_error(crt, sessions, owner, codex):
    codex.plan({"type": "crash"})
    thread = await codex_thread(crt, sessions, owner)
    await crt.send_turn(thread.id, "hi")
    await finished(sessions, thread.id)
    events = await events_of(sessions, thread.id)
    failed = data_of(events, "turn.failed")[0]
    assert failed["error_code"] == "cli_error" and "codex crashed" in failed["error"]
    assert {"text": "half", "incomplete": True} in data_of(events, "assistant.message")


async def test_probe_reports_signed_in_with_live_models(codex):
    probe = await CodexAdapter().probe()
    assert probe.ready and probe.installed and probe.signed_in is True
    assert probe.version == "codex-cli 0.162.1"
    assert [(m.id, m.default) for m in probe.models] == [("gpt-fake", True), ("gpt-fake-mini", False)]
    gpt_fake = probe.models[0]
    assert gpt_fake.default_reasoning_effort == "medium"
    assert gpt_fake.supported_reasoning_efforts == ("low", "medium", "high")
    assert gpt_fake.reasoning_effort_configurable is True


async def test_codex_turn_passes_reasoning_effort(crt, sessions, owner, codex):
    thread = await crt.create_thread(provider="codex", effort="high")
    await crt.send_turn(thread.id, "analyze this deeply")
    await finished(sessions, thread.id)
    turn = codex.received("turn/start")[0]["params"]
    assert turn.get("effort") == "high" 


async def test_probe_reports_signed_out(codex):
    codex.plan(login="out")
    probe = await CodexAdapter().probe()
    assert not probe.ready and probe.installed and probe.signed_in is False
    assert "engine settings" in probe.reason and "terminal" not in probe.reason
    assert "example.com" not in json.dumps(probe.__dict__, default=str)


async def test_probe_with_a_hanging_login_check_gives_up(codex):
    codex.plan(login="hang")
    adapter = CodexAdapter()
    adapter.login_timeout = 0.5
    import time

    started = time.monotonic()
    probe = await adapter.probe()
    assert time.monotonic() - started < 5
    assert not probe.ready and probe.signed_in is None


async def test_probe_without_codex(monkeypatch):
    monkeypatch.setattr(codex_adapter, "resolve_codex_executable", lambda: None)
    probe = await CodexAdapter().probe()
    assert not probe.installed and not probe.ready and "not found" in probe.reason


async def test_team_mcp_create_uses_one_use_gate_with_host_tools_off(crt, sessions, owner, codex, monkeypatch):
    from app.team import service
    from app.db.models import Approval, TeamSetupRequest
    from sqlalchemy import select
    monkeypatch.setattr(settings, 'TEAM_ENABLED', True)
    monkeypatch.setattr(settings, 'TEAM_LEAD_AUTO', True)
    async with sessions() as db:
        lead = await agent_store.create_agent(db, owner_id=owner, name='Coordinator', provider='codex')
        await service.set_lead(db, owner, lead.id)
    codex.plan({'type': 'team', 'tool': 'create_agent', 'arguments': {'name': 'File reader', 'instructions': 'Summarize local files in concise bullets.', 'first_task': 'Summarize the README.'}})
    thread = await crt.create_thread(provider='codex', agent_id=lead.id)
    await crt.send_turn(thread.id, 'Create a file reader agent')
    await finished(sessions, thread.id)
    started = codex.received('thread/start')[0]['params']
    assert started['config']['mcp_servers']['team']['default_tools_approval_mode'] == 'prompt'
    assert started['sandbox'] == 'read-only'  # team access never enables host writes
    assert started['approvalPolicy'] == 'untrusted'  # the CLI must be able to ask about MCP calls
    async with sessions() as db:
        # The tool only prepares a card for review; the specialist exists after the person approves it.
        worker = await db.scalar(select(Agent).where(Agent.owner_id == owner, Agent.name == 'File reader'))
        assert worker is None
        request = await db.scalar(select(TeamSetupRequest).where(TeamSetupRequest.owner_id == owner))
        assert request is not None and request.state == 'pending'
    events = await events_of(sessions, thread.id)
    assert data_of(events, 'team.setup.proposed')


@pytest.mark.parametrize('mode,origin', [('plan', 'person'), ('ask', 'outside'), ('ask', 'routine')])
async def test_codex_team_mutations_preserve_policy_floor(crt, sessions, owner, codex, monkeypatch, mode, origin):
    from app.team import service
    from sqlalchemy import select
    monkeypatch.setattr(settings, 'TEAM_ENABLED', True)
    async with sessions() as db:
        lead = await agent_store.create_agent(db, owner_id=owner, name='Coordinator', provider='codex', approval_mode=mode)
        await service.set_lead(db, owner, lead.id)
    codex.plan({'type': 'team', 'tool': 'create_agent', 'arguments': {'name': 'Forbidden worker', 'instructions': 'Read files.'}})
    thread = await crt.create_thread(provider='codex', agent_id=lead.id)
    await crt.send_turn(thread.id, 'create an agent', team_origin=origin)
    await finished(sessions, thread.id)
    assert codex.answers()[0]['result']['action'] == 'decline'
    async with sessions() as db:
        assert not await db.scalar(select(Agent).where(Agent.owner_id == owner, Agent.name == 'Forbidden worker'))


async def test_native_context_keeps_skill_and_roster_out_of_user_message(crt, sessions, owner, codex, monkeypatch):
    from app.services import agent_skills
    from app.team import service
    monkeypatch.setattr(settings, 'TEAM_ENABLED', True)
    async with sessions() as db:
        lead = await agent_store.create_agent(db, owner_id=owner, name='Writer', provider='codex', memory_enabled=False)
        peer = await agent_store.create_agent(db, owner_id=owner, name='PrivateRosterMarker', provider='codex')
        await service.set_lead(db, owner, lead.id)
        await service.change_member(db, owner, peer.id)
    agent_skills.write_skill(lead.id, "humanizer-zh", "Chinese editing", "PrivateSkillBodyMarker")
    thread = await crt.create_thread(provider="codex", agent_id=lead.id)
    for index, text in enumerate(("Please edit this.", "One more edit."), 1):
        await crt.send_turn(thread.id, text, skills=["humanizer-zh"])
        await finished(sessions, thread.id, count=index)
        start = codex.received("thread/start" if index == 1 else "thread/resume")[-1]["params"]
        turn = codex.received("turn/start")[-1]["params"]
        assert turn["input"] == [{"type":"text","text":text}]
        assert "Team tools are available." in start["developerInstructions"]
        for key, marker in [("skills","PrivateSkillBodyMarker"),("team_state","PrivateRosterMarker")]:
            assert marker not in start["developerInstructions"]
            assert turn["additionalContext"][key]["kind"] == "untrusted"
            assert marker in turn["additionalContext"][key]["value"]
        assert codex.received("initialize")[-1]["params"]["capabilities"]["experimentalApi"] is True
    events = await events_of(sessions, thread.id)
    assert [e.data["prompt_text"] for e in events if e.type == "turn.started"] == ["Please edit this.","One more edit."]
    assert [e.data["skills"] for e in events if e.type == "skill.used"] == [["humanizer-zh"],["humanizer-zh"]]


async def test_steward_deepseek_overrides_only_its_own_session(crt,sessions,owner,codex,tmp_path,monkeypatch):
    from app.core.config import settings
    from app.services import agent_store, steward_deepseek
    from app.team import service
    from app.services.thread_runtime import ProviderUnavailable
    monkeypatch.setattr(settings,"STEWARD_STRICT_OWNER",owner)
    monkeypatch.setattr(settings,"TEAM_ENABLED",True)
    monkeypatch.setattr(codex_adapter,"deepseek_home",lambda:tmp_path/"private-ds")
    async with sessions() as db:
        lead = await agent_store.create_agent(db,owner_id=owner,name="lead",provider="codex",model="deepseek-flash")
        await service.set_lead(db,owner,lead.id)
    thread = await crt.create_thread(agent_id=lead.id,provider="codex",model="deepseek-flash")
    monkeypatch.setattr(steward_deepseek,"api_key",lambda:"")
    with pytest.raises(ProviderUnavailable,match="DeepSeek"):
        await crt.send_turn(thread.id,"test")
    assert not codex.received("turn/start")
    monkeypatch.setattr(steward_deepseek,"api_key",lambda:"test-only-credential")
    codex.plan({"type":"chat","text":"answered"},login="out")
    await crt.send_turn(thread.id,"hello")
    await finished(sessions,thread.id)
    first = codex.received("thread/start")[0]["params"]
    assert first["modelProvider"] == "suparagent_deepseek" and first["model"]=="deepseek-flash"
    cfg = first["config"]["model_providers.suparagent_deepseek"]
    assert cfg["wire_api"] == "responses" and cfg["base_url"] == "https://api.deepseek.com/"
    assert cfg["experimental_bearer_token"] == "test-only-credential"
    assert not cfg["requires_openai_auth"] and first["config"]["web_search"] == "disabled"
    assert first["config"]["mcp_servers"]["team"]["command"]
    assert first["config"]["features"]["shell_tool"] is False
    assert first["environments"] == []
    assert codex.received("turn/start")[-1]["params"]["environments"] == []
    row = await reload(sessions,Thread,thread.id)
    assert row.provider_state["steward_deepseek"] is True
    assert "test-only-credential" not in json.dumps(row.provider_state)
    await crt.send_turn(thread.id,"continue")
    await finished(sessions,thread.id,count=2)
    assert codex.received("thread/resume")[-1]["params"]["modelProvider"]=="suparagent_deepseek"
    codex.plan({"type": "fail", "message": "invalid test-only-credential"})
    await crt.send_turn(thread.id, "simulate invalid provider credentials")
    await finished(sessions, thread.id, count=3)
    errors = data_of(await events_of(sessions, thread.id), "turn.failed")
    assert errors[-1]["error"] == "invalid [redacted]"
    assert crt.engine_limit("codex") is None
    async with sessions() as db:
        (await db.get(Thread,thread.id)).model="gpt-fake"
        await db.commit()
    with pytest.raises(ProviderUnavailable,match="新会话"):
        await crt.send_turn(thread.id,"switch provider")


async def test_steward_gpt_turns_use_backend_history_without_desktop_sessions(crt, sessions, owner, codex, monkeypatch):
    thread = await codex_thread(crt, sessions, owner)
    monkeypatch.setattr(settings, "STEWARD_STRICT_OWNER", owner)
    await crt.send_turn(thread.id, "first question")
    await finished(sessions, thread.id)
    await crt.send_turn(thread.id, "continue")
    await finished(sessions, thread.id, count=2)
    assert not codex.received("thread/resume")
    starts = [r["params"] for r in codex.received("thread/start")]
    assert len(starts) == 2 and all(r["ephemeral"] for r in starts)
    assert all(r["config"]["features.multi_agent"] is False for r in starts)
    state = (await reload(sessions, Thread, thread.id)).provider_state
    assert state["service_history"] and "codex_thread_id" not in state
    context = codex.received("turn/start")[-1]["params"]["additionalContext"]["prior_conversation"]
    assert context["kind"] == "untrusted"
    assert [r["text"] for r in json.loads(context["value"])] == ["first question", "hello there"]

@pytest.mark.parametrize(("role", "mode", "tool", "allowed"), [
    ("employee", "ask", "use_project_release", True),
    ("employee", "ask", "download_task_reference", True),
    ("employee", "ask", "deliver_project_file", True),
    ("employee", "plan", "read_task_reference", True),
    ("employee", "plan", "download_task_reference", False),
    ("employee", "plan", "deliver_project_file", False),
    ("employee", "plan", "use_project_release", False),
    ("employee", "ask", "publish_shared_release", False),
    ("steward", "ask", "publish_shared_release", True),
    ("ordinary", "ask", "publish_shared_release", False),
    ("steward", "ask", "deliver_project_file", False),
])
async def test_approval_uses_the_catalog_advertised_to_this_role(tmp_path, role, mode, tool, allowed):
    from unittest.mock import AsyncMock, Mock
    from app.team.policy import TurnTeam
    employee = role == "employee"
    ctx = TurnTeam(1, 2, role == "steward", False, strict_hierarchy=True,
                   feature_consult=employee, delegation_enabled=False)
    turn = _turn(mode, project_runtime=SimpleNamespace(root=tmp_path,writable=mode!="plan") if employee else None,
                 feature_workspace=str(tmp_path) if employee else None, team=ctx,
                 team_launch=SimpleNamespace(args=["--steward"] if role=="steward" else []))
    turn._emit, turn.rpc, turn.gate = AsyncMock(return_value=True), Mock(), SimpleNamespace(open=AsyncMock(return_value=None))
    args = {"fixture": "value"}
    await turn._item_started({"id":"item", "type":"mcpToolCall", "server":"team","tool":tool,"arguments":args})
    await turn._server_request({"id":1,"method":"mcpServer/elicitation/request","params":{
        "serverName":"team", "message":f'run tool "{tool}"', "_meta":{"codex_approval_kind":"mcp_tool_call","tool_params":args}}})
    if allowed:
        assert turn.gate.open.await_args.kwargs["tool"] == "mcp__team__" + tool
    else:
        turn.gate.open.assert_not_awaited()
        turn.rpc.respond.assert_called_once_with(1, {"action":"decline"})
