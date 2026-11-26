"""Execute one reviewed employee command using the bound App Server's named permissions."""
import asyncio
from pathlib import PureWindowsPath
import subprocess
from uuid import uuid4

from app.core import app_settings
from app.services.thread_store import cap_tool_output
from app.team import work_verification
from app.team.mcp_server import MAILBOX_TIMEOUT_SECONDS


def available(ctx, *, live=False):
    project = getattr(ctx, "project_runtime", None)
    team = getattr(ctx, "team", None)
    if (not project or not project.writable or ctx.approval_mode != "ask" or not team or team.is_lead
            or team.origin != "person" or not team.feature_consult or not team.strict_hierarchy
            or getattr(ctx, "consult_only", False) or getattr(ctx, "scheduled_knowledge", False)):
        return False
    # Explicit operator settings at decision time also override a turn's captured default.
    setting = app_settings.resolve("LOCAL_TOOLS_ENABLED", live=live)
    return setting.effective_value is True or (setting.source == "default" and not setting.error)


def checked_command(project, args):
    if not isinstance(args, dict) or set(args) - {"argv", "cwd"}:
        raise ValueError("项目命令只接受完整参数和项目内的工作目录。")
    argv, cwd = args.get("argv"), args.get("cwd", ".")
    if (not isinstance(argv, list) or not 1 <= len(argv) <= 128 or not argv[0]
            or any(not isinstance(arg,str) or "\x00" in arg or len(arg)>8192 for arg in argv)
            or sum(map(len,argv))>16000 or not isinstance(cwd,str) or not cwd or PureWindowsPath(cwd).anchor):
        raise ValueError("命令参数或相对目录无效。")
    directory, relative = work_verification._project_path(cwd, project.root)
    if not directory.is_dir():
        raise ValueError("项目内的命令工作目录不存在。")
    return list(argv), directory, relative


def _declared_files(project, check):
    result = {}
    for item in (check or {}).get("files", ()):
        try:
            path, relative = work_verification._project_path(item["path"], project.root)
            result[relative] = work_verification._content_digest(path) if path.exists() else None
        except (KeyError, OSError, TypeError, ValueError):
            continue
    return result


async def execute(run, args, check):
    from app.services.stdio_rpc import RpcClosed, RpcError
    if (not available(run.ctx, live=True) or run.ctx.aborted or run.completed or run.sink.lost or not run.rpc
            or run.rpc.closed or not run.thread_id or not run.codex_turn_id):
        raise ValueError("此员工当前没有可用的项目命令会话。")
    argv, cwd, relative = checked_command(run.project, args)
    # File evidence is limited to deliverables that the dispatch already names.
    before = await asyncio.to_thread(_declared_files, run.project, check)
    process_id = "employee-" + uuid4().hex
    display = subprocess.list2cmdline(argv)
    # This public RPC launches argv directly in the named profile. It has no
    # implicit retry outside that profile and does not answer a native escalation.
    limit = MAILBOX_TIMEOUT_SECONDS - 15
    remaining = run.clock.remaining()
    budget = min(limit, remaining) if remaining is not None else limit
    if budget <= 0:
        raise ValueError("本轮时间已到，命令没有启动。")
    params = {"command":argv, "cwd":str(cwd), "permissionProfile":run.project.permission_name,
              "timeoutMs":max(1,int(budget*1000)), "outputBytesCap":32_768, "processId":process_id}
    if run.ctx.aborted or run.sink.lost:
        raise ValueError("派工已停止，命令没有启动。")
    if not await run._emit("tool.started", {"call_id":process_id,"name":"Bash","summary":display[:200],
            "input":{"command":display,"argv":argv,"cwd":str(cwd)}}):
        raise ValueError("无法记录这条项目命令，未启动执行。")
    try:
        if run.ctx.aborted or run.sink.lost:
            raise ValueError("派工已结束，命令没有启动。")
        result = await run.rpc.request("command/exec", params, timeout=budget+10)
    except (RpcError, RpcClosed, asyncio.TimeoutError) as exc:
        await run._emit("tool.completed", {"call_id":process_id,"ok":False,"truncated":False,
            "output":"员工引擎未返回退出结果。"})
        raise ValueError("员工引擎未返回这条命令的退出结果，请核对工单后再试。") from exc
    if (not isinstance(result,dict) or type(result.get("exitCode")) is not int
            or not isinstance(result.get("stdout"),str) or not isinstance(result.get("stderr"),str)):
        raise ValueError("未收到完整的项目命令回执。")
    after = await asyncio.to_thread(_declared_files, run.project, check)
    code = result["exitCode"]
    root = str(run.project.root)
    changed = [{"scope":"role", "path":name, "root":root, "sha256":digest, "operation":"changed_during_command"}
               for name,digest in after.items() if digest and name in before and digest!=before[name]]
    output, truncated = cap_tool_output("\n".join(s for s in (result["stdout"], result["stderr"]) if s))
    command = {"command":display,"argv":argv,"cwd":relative,"root":root,"exit_code":code,
               "status":"completed" if code==0 else "failed","call_id":process_id}
    recorded = await run._emit("tool.completed", {"call_id":process_id,"ok":code==0,"output":output,
        "truncated":truncated,"command_receipt":command,"file_receipts":changed})
    if not recorded:
        raise ValueError("命令已结束，工单未能保存运行回执，请先核对结果。")
    return {"exit_code":code,"stdout":result["stdout"],"stderr":result["stderr"]}
