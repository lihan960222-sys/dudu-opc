from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from app.services import employee_terminal
from app.team import mcp_server, policy


def project_context(root):
    profile = NS(root=root, writable=True, permission_name="employee-qa", task_id=4)
    team = policy.TurnTeam(3, 9, False, False, feature_consult=True, strict_hierarchy=True)
    return NS(project_runtime=profile, approval_mode="ask", team=team, scheduled_knowledge=False,
              consult_only=False, aborted=False, turn_id=6)


def test_new_employee_runs_use_native_tools_and_keep_external_integrations(tmp_path):
    from app.services import codex_adapter
    from app.team.bridge import TeamBridge

    ctx = project_context(tmp_path)
    from dataclasses import replace
    ctx.team = replace(ctx.team, feishu_tools=("feishu_create_document",))
    bridge = TeamBridge(NS(), NS(ctx=ctx, turn_id=ctx.turn_id))
    try:
        flags = bridge.launch.args
        assert set(flags).isdisjoint({"--project-command", "--project-edit", "--project-read",
                                      "--local-inspection", "--project-create", "--peer-files"})
        catalog = set(codex_adapter.feature_team_tools(ctx))
        assert {"run_project_command", "create_project_text", "replace_project_text", "list_project_files",
                "read_project_text", "inspect_local_path"}.isdisjoint(catalog)
        assert {"employee_capabilities", "feishu_create_document", "read_task_reference",
                "download_task_reference", "deliver_project_file", "use_project_release"} <= catalog
    finally:
        bridge.folder.rmdir()


def test_reviewed_commands_require_an_employee_work_order(tmp_path):
    ctx = project_context(tmp_path)
    assert employee_terminal.available(ctx)
    assert policy.automatic("mcp__team__run_project_command", "ask", ctx.team, {"argv":["cmd.exe"]}) is None
    server = mcp_server.Server(tmp_path, project_consult=True, project_command=True)
    assert "run_project_command" in {item["name"] for item in server.tools}
    assert "run_project_command" not in {item["name"] for item in mcp_server.Server(tmp_path).tools}
    for changes in ({"approval_mode":"plan"}, {"scheduled_knowledge":True}, {"project_runtime":None},
                    {"consult_only":True}, {"team":None}):
        assert not employee_terminal.available(NS(**{**vars(ctx), **changes}))


@pytest.mark.asyncio
async def test_scoped_command_uses_real_argv_and_named_profile(tmp_path):
    from app.services import codex_adapter
    ctx = project_context(tmp_path)
    script = tmp_path / "seconds.cmd"
    script.write_text("before", encoding="utf8")
    async def rpc(method, params, **kwargs):
        assert method == "command/exec" and params["permissionProfile"] == "employee-qa"
        assert set(params) == {"command","cwd","permissionProfile","outputBytesCap","processId","timeoutMs"}
        assert params["cwd"] == str(tmp_path) and params["command"][1:] == ["/d","/c","verify.cmd"]
        script.write_text("after", encoding="utf8")
        return {"exitCode": 1, "stdout": "fixture assertion", "stderr": ""}
    run = NS(ctx=ctx, project=ctx.project_runtime, clock=NS(remaining=lambda:30), rpc=NS(request=AsyncMock(side_effect=rpc),closed=False),
             completed=False, thread_id="native",codex_turn_id="turn", sink=NS(lost=False), _emit=AsyncMock(return_value=True))
    args = {"argv":["cmd.exe","/d","/c","verify.cmd"], "cwd":"."}
    adapter = codex_adapter.CodexAdapter()
    adapter._active_turns[ctx.turn_id] = run
    result = await adapter.run_project_command(ctx,args,{"files":[{"scope":"role","path":"seconds.cmd"}]})
    assert result["exit_code"] == 1 and result["stdout"] == "fixture assertion"
    event = run._emit.await_args.args[1]
    assert event["command_receipt"]["command"] == "cmd.exe /d /c verify.cmd"
    assert event["command_receipt"]["exit_code"] == 1 and not event["ok"]
    assert event["file_receipts"][0]["path"] == "seconds.cmd"
    for changed in ({"cwd":".."}, {"cwd":str(tmp_path.parent)}, {"argv":[]}, {"permissionProfile":"unsafe"}):
        with pytest.raises(ValueError):
            await adapter.run_project_command(ctx,{**args,**changed},{})
    ctx.aborted=True
    with pytest.raises(ValueError):
        await adapter.run_project_command(ctx,args,{})
    assert run.rpc.request.await_count == 1
