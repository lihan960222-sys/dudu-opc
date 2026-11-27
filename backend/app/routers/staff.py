"""Owner-only host profile operations for the local management workbench."""
import base64
import binascii
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, StrictBool
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from app.auth.middleware import principal_of
from app.core import tenant_paths
from app.db.models import Agent, AgentJob, Thread, Turn
from app.db.models.team import TeamSetupRequest
from app.db.transactions import begin_write
from app.routers.agents import _agent_or_404, _restricted, get_runtime, _check_engine
from app.routers.employee_codex_login import router as employee_codex_login_router
from app.routers.shared_library import router as shared_library_router
from app.routers.history_reset import router as history_reset_router
from app.routers.feishu_access import router as feishu_access_router
from app.routers.weekly_knowledge import router as weekly_knowledge_router
from app.routers.feishu_owner_oauth import owner_browser, signed_in_owner_browser
from app.services import agent_store, image_gen, role_workspace, steward_deepseek
from app.services.local_access_policy import require_local_access
from app.services.thread_runtime import ThreadRuntime
from app.team import service as team_service
from app.team.setup import public_plan, request_version


def local_owner(request: Request):
    person = principal_of(request.scope)
    if (_restricted(request) or tenant_paths.root() is not None
            or (person and person.person_id is not None and person.role != 'owner')):
        raise HTTPException(403, "需要本机所有者权限")


router = APIRouter(prefix="/staff", dependencies=[Depends(require_local_access), Depends(local_owner)])
router.include_router(employee_codex_login_router)
router.include_router(feishu_access_router)
router.include_router(weekly_knowledge_router)
router.include_router(shared_library_router)
router.include_router(history_reset_router)


class StaffingGrant(BaseModel):
    can_staff_team: StrictBool


class ProjectTerminalSetting(BaseModel):
    enabled: StrictBool
    read_folders: list[str] | None = Field(None, max_length=32)
    outbound_network: StrictBool | None = None

    model_config = {"extra": "forbid"}


async def _terminal_employee(db, owner, agent_id, *, lock=False):
    team = await team_service.team_for(db, owner, lock=lock)
    query = select(Agent).where(Agent.id == agent_id, Agent.owner_id == owner, Agent.archived_at.is_(None))
    agent = await db.scalar(query.with_for_update() if lock else query)
    if not agent:
        raise HTTPException(404, "未找到这名员工。")
    if team and team.lead_agent_id == agent.id:
        raise HTTPException(409, "项目终端用于受派工的专业员工。")
    return agent


def _terminal_response(owner, choice):
    from app.core.config import settings
    workstation = bool(owner and owner == settings.STEWARD_STRICT_OWNER)
    return {"enabled": choice.enabled, "read_folders": choice.read_folders,
            "outbound_network": choice.outbound_network, "local_workstation": workstation}


@router.get("/{agent_id}/project-terminal", dependencies=[Depends(signed_in_owner_browser)])
async def project_terminal_status(agent_id: int, rt: ThreadRuntime = Depends(get_runtime)):
    from app.services import employee_runtime
    async with rt.session() as db:
        await _terminal_employee(db, rt.owner_id, agent_id)
    try:
        choice = employee_runtime.terminal_settings(rt.owner_id, agent_id)
    except (ValueError, OSError) as exc:
        raise HTTPException(409, str(exc)) from None
    return JSONResponse(_terminal_response(rt.owner_id, choice), headers={"Cache-Control": "no-store"})


@router.put("/{agent_id}/project-terminal", dependencies=[Depends(signed_in_owner_browser)])
async def set_project_terminal(agent_id: int, choice: ProjectTerminalSetting, rt: ThreadRuntime = Depends(get_runtime)):
    from app.services import employee_runtime, feature_tasks
    from app.team.jobs import LIVE
    async with rt.session() as db:
        await begin_write(db)
        # Dispatch uses the same team lock. An accepted job keeps its execution mode until it ends.
        agent = await _terminal_employee(db, rt.owner_id, agent_id, lock=True)
        try:
            current = employee_runtime.terminal_settings(rt.owner_id, agent_id)
            updated = employee_runtime.TerminalSettings(choice.enabled,
                current.read_folders if choice.read_folders is None else tuple(dict.fromkeys(p.strip() for p in choice.read_folders)),
                current.outbound_network if choice.outbound_network is None else choice.outbound_network)
            if updated != updated.effective(rt.owner_id):
                raise HTTPException(422, "本工作台已统一管理员工的本地查阅和联网范围。")
            if current != updated:
                job = await db.scalar(select(AgentJob.id).where(AgentJob.owner_id == rt.owner_id,
                    AgentJob.assignee_agent_id == agent_id, AgentJob.kind == "job", AgentJob.mode == "work",
                    AgentJob.task_id.is_not(None), AgentJob.status.in_((*LIVE, "dispatched"))).limit(1))
                turn = await db.scalar(select(Turn.id).join(Thread, Thread.id == Turn.thread_id).where(
                    Thread.owner_id == rt.owner_id, Thread.agent_id == agent_id,
                    Thread.kind == "feature", Turn.status == "running").limit(1))
                if job or turn:
                    raise HTTPException(409, "这名员工还有项目工单在办理。请等工单结束或先停止任务，再切换项目终端。")
            if choice.enabled:
                if not agent.role_path:
                    raise HTTPException(422, "请先保存这名员工自己的工作目录。")
                root = role_workspace.checked_root(Path(agent.role_path))
                role_workspace.load(root, agent.model)
                await feature_tasks.check_employee_root(db, rt.owner_id, agent, root, enabling=True)
            if updated.read_folders and (updated.enabled or updated.read_folders != current.read_folders):
                from app.services import team_workspaces
                directory = await team_workspaces.load(db, None)
                updated.validate(directory.protected_roots)
            employee_runtime.save_terminal_settings(rt.owner_id, agent_id, updated)
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from None
        await db.commit()
    return _terminal_response(rt.owner_id, updated)


@router.patch("/{agent_id}/staffing", dependencies=[Depends(owner_browser)])
async def set_staffing_grant(agent_id: int, grant: StaffingGrant, rt: ThreadRuntime = Depends(get_runtime)):
    async with rt.session() as db:
        await begin_write(db)
        team = await team_service.team_for(db, rt.owner_id, lock=True)
        if not team or team.lead_agent_id != agent_id:
            raise HTTPException(409, "只有当前大管家可以获得招人授权。")
        agent = await db.scalar(select(agent_store.Agent).where(
            agent_store.Agent.id == agent_id, agent_store.Agent.owner_id == rt.owner_id,
            agent_store.Agent.team_id == team.id, agent_store.Agent.archived_at.is_(None)
        ).with_for_update())
        if agent is None:
            raise HTTPException(404, "未找到大管家。")
        if agent.can_staff_team != grant.can_staff_team:
            agent.can_staff_team = grant.can_staff_team
            team_service.audit(db, team, "staffing_granted" if grant.can_staff_team else "staffing_revoked", agent_id=agent.id)
        await db.commit()
        return {"can_staff_team": grant.can_staff_team}


@router.get("/deepseek-key", dependencies=[Depends(owner_browser)])
def deepseek_status():
    return JSONResponse({"configured": bool(steward_deepseek.api_key())}, headers={"Cache-Control": "no-store"})


@router.put("/deepseek-key", dependencies=[Depends(owner_browser)])
async def save_deepseek_key(request: Request):
    key = await _submitted_key(request)
    try:
        await run_in_threadpool(steward_deepseek.verify_and_save, key)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return JSONResponse({"configured": True}, headers={"Cache-Control": "no-store"})


async def _submitted_key(request: Request) -> str:
    # A validation error for a credential must never include the submitted value.
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 2048:
            raise HTTPException(413, "密钥请求过大。")
        raw.extend(chunk)
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        payload = None
    if not isinstance(payload, dict) or set(payload) != {"api_key"} or not isinstance(payload["api_key"], str):
        raise HTTPException(422, "请输入 DeepSeek API Key。")
    return payload["api_key"]


@router.get("/{agent_id}/deepseek-key", dependencies=[Depends(owner_browser)])
async def employee_key_status(agent_id: int, rt: ThreadRuntime = Depends(get_runtime)):
    await _agent_or_404(rt, agent_id)
    models = await run_in_threadpool(steward_deepseek.employee_models, rt.owner_id, agent_id)
    return JSONResponse({"configured": bool(models), "models": models}, headers={"Cache-Control": "no-store"})


@router.put("/{agent_id}/deepseek-key", dependencies=[Depends(owner_browser)])
async def save_employee_key(agent_id: int, request: Request, rt: ThreadRuntime = Depends(get_runtime)):
    await _agent_or_404(rt, agent_id)
    key = await _submitted_key(request)
    try:
        await run_in_threadpool(steward_deepseek.save_employee_key, rt.owner_id, agent_id, key)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return JSONResponse({"configured": True}, headers={"Cache-Control": "no-store"})


@router.get("/folders")
def folders(path: str | None = None):
    try:
        directory = role_workspace.checked_root(Path(path) if path else Path.home())
        children = []
        for entry in directory.iterdir():
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir() and not role_workspace.linked(entry):
                    children.append(entry.name)
            except OSError:
                continue
        return {"path": str(directory), "parent": str(directory.parent) if directory.parent != directory else None,
                "folders": sorted(children, key=str.casefold)}
    except (ValueError, OSError) as exc:
        raise HTTPException(422, str(exc)) from None


@router.get("/pending")
async def pending(rt: ThreadRuntime = Depends(get_runtime)):
    async with rt.session() as db:
        rows = await db.scalars(select(TeamSetupRequest).where(TeamSetupRequest.owner_id == rt.owner_id,
            TeamSetupRequest.state == "pending", TeamSetupRequest.kind == "setup").order_by(TeamSetupRequest.id.desc()))
        return [{"requestId": r.request_id, "version": request_version(r), "plan": public_plan(r.plan),
                 "threadId": r.thread_id, "turnId": r.turn_id} for r in rows]


class EditRules(BaseModel):
    path: str
    digest: str = Field(pattern="^[a-f0-9]{64}$")
    model: str = Field(pattern=r"^[\w./:@+\[\]-]+$", max_length=200)
    instructions: str = Field(max_length=65536)
    provider: str | None = Field(default=None, max_length=64)
    model_vendor: str | None = Field(default=None, max_length=64)
    effort: str | None = Field(default=None, max_length=64)


def read_rules(root, fallback_model=None):
    role = role_workspace.load(root, fallback_model)
    if (root / "AGENTS.md").stat().st_nlink != 1:
        raise ValueError("工作指令文件不能使用硬链接")
    return {"path": str(root), "digest": role.digest, "model": role.model, "instructions": role.instructions}


def write_rules(root, edit, fallback_model=None):
    current = read_rules(root, fallback_model)
    if str(root) != edit.path or current["digest"] != edit.digest:
        raise HTTPException(409, "档案已被修改，请重新打开后编辑")
    path = root / "AGENTS.md"
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != edit.digest:
        raise HTTPException(409, "文件已被修改")
    text = raw.decode("utf-8-sig")
    end = re.search(r"(?m)^---\r?$", text[3:])
    has_header = re.match(r"^---\r?\n", text) and end
    if has_header:
        head = text[:3 + end.end()].rstrip("\r")
    else:
        head = f"---\ndefault_model: {edit.model}\n---"
    head = re.sub(r"(?m)^([ \t]*default_model:[ \t]+)\S+", lambda m: m[1] + edit.model, head)
    newline = "\r\n" if "\r\n" in text else "\n"
    if edit.instructions == current["instructions"]:
        after = text[3 + end.end():] if has_header else newline + text
        if has_header and newline == "\r\n" and after.startswith("\n"):
            after = "\r" + after
    else:
        after = newline + edit.instructions.replace("\r\n", "\n").replace("\n", newline)
    result = (head + after).encode("utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8")
    if len(result) > 65536:
        raise ValueError("文件不能超过 64 KiB")
    with tempfile.NamedTemporaryFile(dir=root, delete=False) as tmp:
        temp = Path(tmp.name)
        try:
            tmp.write(result)
        except BaseException:
            tmp.close()
            temp.unlink(missing_ok=True)
            raise
    try:
        if read_rules(root, fallback_model)["digest"] != edit.digest:
            raise HTTPException(409, "文件已被修改")
        os.chmod(temp, path.stat().st_mode)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return read_rules(root, fallback_model)


@router.get("/{agent_id}/rules")
async def rules(agent_id: int, rt: ThreadRuntime = Depends(get_runtime)):
    agent = await _agent_or_404(rt, agent_id)
    if not agent.role_path:
        raise HTTPException(409, "请先设置工作目录")
    try:
        return read_rules(Path(agent.role_path), agent.model)
    except (OSError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from None


@router.put("/{agent_id}/rules")
async def update_rules(agent_id: int, edit: EditRules, rt: ThreadRuntime = Depends(get_runtime)):
    async with rt.session() as db:
        await begin_write(db)
        agent = await db.scalar(select(agent_store.Agent).where(agent_store.Agent.id == agent_id,
            agent_store.Agent.owner_id == rt.owner_id).with_for_update())
        if not agent:
            raise HTTPException(404, "找不到员工")
        if agent.role_path != edit.path:
            raise HTTPException(409, "工作目录已更改")
        from app.routers.agents import _profile_vendor
        provider = edit.provider or agent.provider
        effort = edit.effort if "effort" in edit.model_fields_set else agent.effort if agent.model == edit.model else None
        explicit = edit.model_vendor
        if "model_vendor" not in edit.model_fields_set and (provider, edit.model) == (agent.provider, agent.model):
            explicit = agent.model_vendor
        vendor = _profile_vendor(provider, explicit, edit.model, effort)
        _check_engine(rt, provider, edit.model)
        if provider != agent.provider:
            if await rt.computers.for_agent(agent.id) and not rt.engines.capabilities(provider).computers:
                raise HTTPException(409, "该员工绑定的计算机不支持所选引擎。")
            team = await team_service.team_for(db, rt.owner_id)
            if team and team.lead_agent_id == agent.id and not rt.engines.capabilities(provider).mcp:
                raise HTTPException(409, "管家需要支持团队工具的引擎。")
            if agent.connector_scopes and not rt.engines.capabilities(provider).connected_apps:
                raise HTTPException(409, "该引擎不支持员工已授权的应用。")
        try:
            result = write_rules(Path(agent.role_path), edit, agent.model)
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from None
        agent.model = edit.model
        agent.provider, agent.model_vendor, agent.effort = provider, vendor, effort
        agent.instructions = result["instructions"]
        await db.commit()
        return result


class Avatar(BaseModel):
    png: str = Field(max_length=720000)


@router.put("/{agent_id}/picture", status_code=204)
async def picture(agent_id: int, payload: Avatar, rt: ThreadRuntime = Depends(get_runtime)):
    await _agent_or_404(rt, agent_id)
    try:
        data = base64.b64decode(payload.png, validate=True)
    except (ValueError, binascii.Error):
        raise HTTPException(422, "无效图片") from None
    if image_gen.sniff(data) != ("image/png", ".png") or len(data) < 33 or data[12:16] != b"IHDR" or data[16:24] != (192).to_bytes(4,"big") * 2:
        raise HTTPException(422, "需要 192 × 192 的 PNG 头像")
    folder = image_gen.avatars_root()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(dir=folder, delete=False) as tmp:
        name = Path(tmp.name)
        try:
            tmp.write(data)
        except BaseException:
            tmp.close()
            name.unlink(missing_ok=True)
            raise
    try:
        os.replace(name, folder / f"{agent_id}.png")
    finally:
        name.unlink(missing_ok=True)

@router.get("/projects/index")
async def projects(rt: ThreadRuntime = Depends(get_runtime)):
    from app.db.models import WorkItem, FeatureScope
    async with rt.session() as db:
        rows = await db.execute(select(WorkItem.id, FeatureScope.project).join(
            FeatureScope, FeatureScope.task_id == WorkItem.id).where(
            WorkItem.owner_id == rt.owner_id, WorkItem.archived.is_(False)))
        return [{"task_id": task, "project": project} for task, project in rows]
