"""Device authorization for a single employee's service-owned OpenAI profile."""
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select

from app.core import host_exec
from app.db.models import Agent, Team
from app.routers.agents import get_runtime
from app.routers.feishu_owner_oauth import owner_browser
from app.services import codex_adapter, employee_runtime, engine_registry
from app.services.codex_login import CodexLogin, LoginError, account_signed_in, employee_credentials_exist
from app.services.thread_runtime import ThreadRuntime


router = APIRouter(prefix="/{agent_id}/openai")


async def _employee(agent_id: int, rt: ThreadRuntime = Depends(get_runtime), principal=Depends(owner_browser)):
    async with rt.session() as db:
        agent = await db.scalar(select(Agent).where(Agent.id == agent_id, Agent.owner_id == rt.owner_id,
                                                    Agent.archived_at.is_(None)))
        if agent is None:
            raise HTTPException(404, "找不到这名员工。")
        team = await db.get(Team, agent.team_id) if agent.team_id else None
        if team and team.lead_agent_id == agent.id:
            raise HTTPException(409, "此入口供专业员工使用。")
        try:
            profile = engine_registry.resolve_model_profile(agent.provider, agent.model_vendor, agent.model, agent.effort)
        except engine_registry.InvalidModel:
            profile = None
        if not profile or (profile.engine_id, profile.model_vendor) != ("codex", "openai"):
            raise HTTPException(409, "请先在这名员工的档案中保存 Codex 引擎和 OpenAI 模型。")
    return f"{principal.kind}:{principal.session_id}"


async def _private_home(rt, agent_id):
    async with rt.session() as db:
        paths = (await db.execute(select(Agent.role_path, Agent.workspace_path))).all()
    employee_folders = {Path(value).expanduser().resolve() for row in paths for value in row if value}
    home = employee_runtime.profile_dir(rt.owner_id, agent_id, "openai")
    shared = os.environ.get("CODEX_HOME")
    protected = {*employee_folders, Path.home() / ".codex"}
    if shared:
        protected.add(Path(shared).expanduser().resolve())
    if any(home.is_relative_to(folder) or folder.is_relative_to(home) for folder in protected):
        raise LoginError("员工的登录目录与其他档案重叠。")
    return home, employee_folders


def _connection(rt, agent_id, home, employee_folders):
    if rt._closing or "codex" not in rt.adapters:
        raise LoginError("当前后台无法连接 Codex。")
    executable = codex_adapter.resolve_employee_executable()
    if executable is None:
        raise LoginError("请先在主机安装 Codex。")
    employee_runtime.check_service_executable(executable, employee_folders)
    if reason := host_exec.employee_host_blocker():
        raise LoginError(reason)
    env = employee_runtime.profile_environment(rt.owner_id, agent_id, "openai")
    if Path(env["CODEX_HOME"]) != home:
        raise LoginError("员工的登录目录已更改，请重新打开档案。")
    return executable, env


def _unavailable(exc):
    return HTTPException(409, str(exc) if isinstance(exc, (LoginError, employee_runtime.BoundaryUnavailable))
                         else "无法核验此员工的独立 OpenAI 登录目录。")


@router.get("")
async def status(agent_id: int, response: Response, browser=Depends(_employee), rt: ThreadRuntime = Depends(get_runtime)):
    response.headers["Cache-Control"] = "no-store"
    manager = rt.employee_codex_logins.get((rt.owner_id, agent_id))
    if manager and (flow := manager.pending(browser)):
        return {"signed_in": False, "login": flow}
    try:
        home, folders = await _private_home(rt, agent_id)
        if not employee_credentials_exist(home):
            return {"signed_in": False}
        executable, env = _connection(rt, agent_id, home, folders)
        return {"signed_in": await account_signed_in(executable, env, employee=True)}
    except (OSError, ValueError) as exc:
        raise _unavailable(exc) from None


@router.post("/login", status_code=202)
async def start(agent_id: int, request: Request, response: Response, browser=Depends(_employee),
                rt: ThreadRuntime = Depends(get_runtime)):
    response.headers["Cache-Control"] = "no-store"
    if request.query_params:
        raise HTTPException(422, "员工登录只接受当前档案，无需提交参数。")
    async for chunk in request.stream():
        if chunk:
            raise HTTPException(422, "员工登录只接受当前档案，无需提交参数。")
    try:
        home, folders = await _private_home(rt, agent_id)
        employee_credentials_exist(home)
        executable, env = _connection(rt, agent_id, home, folders)
        manager = rt.employee_codex_logins.setdefault((rt.owner_id, agent_id), CodexLogin())
        return await manager.start(browser, executable, employee_env=env)
    except (OSError, ValueError) as exc:
        raise _unavailable(exc) from None


def _flow(rt, agent_id, browser, flow_id):
    manager = rt.employee_codex_logins.get((rt.owner_id, agent_id))
    try:
        if manager:
            return manager, manager.view(browser, flow_id)
    except KeyError:
        pass
    raise HTTPException(404, "找不到本次员工登录。")


@router.get("/login/{flow_id}")
async def progress(agent_id: int, flow_id: str, response: Response, browser=Depends(_employee),
                   rt: ThreadRuntime = Depends(get_runtime)):
    response.headers["Cache-Control"] = "no-store"
    _, view = _flow(rt, agent_id, browser, flow_id)
    return view


@router.delete("/login/{flow_id}", status_code=204)
async def cancel(agent_id: int, flow_id: str, response: Response, browser=Depends(_employee),
                 rt: ThreadRuntime = Depends(get_runtime)):
    response.headers["Cache-Control"] = "no-store"
    manager, _ = _flow(rt, agent_id, browser, flow_id)
    await manager.cancel(browser, flow_id)
