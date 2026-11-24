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
from pydantic import BaseModel, Field
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from app.auth.middleware import principal_of
from app.core import tenant_paths
from app.db.models.team import TeamSetupRequest
from app.db.transactions import begin_write
from app.routers.agents import _agent_or_404, _restricted, get_runtime, _check_engine
from app.routers.feishu_access import router as feishu_access_router
from app.routers.feishu_owner_oauth import owner_browser
from app.services import agent_store, image_gen, role_workspace, steward_deepseek
from app.services.local_access_policy import require_local_access
from app.services.thread_runtime import ThreadRuntime
from app.team.setup import public_plan, request_version


def local_owner(request: Request):
    person = principal_of(request.scope)
    if (_restricted(request) or tenant_paths.root() is not None
            or (person and person.person_id is not None and person.role != 'owner')):
        raise HTTPException(403, "需要本机所有者权限")


router = APIRouter(prefix="/staff", dependencies=[Depends(require_local_access), Depends(local_owner)])
router.include_router(feishu_access_router)


@router.get("/deepseek-key", dependencies=[Depends(owner_browser)])
def deepseek_status():
    return JSONResponse({"configured": bool(steward_deepseek.api_key())}, headers={"Cache-Control": "no-store"})


@router.put("/deepseek-key", dependencies=[Depends(owner_browser)])
async def save_deepseek_key(request: Request):
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
    try:
        await run_in_threadpool(steward_deepseek.verify_and_save, payload["api_key"])
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
    if re.match(r"^---\r?\n", text) and end:
        head = text[:3 + end.end()].rstrip("\r")
    else:
        head = f"---\ndefault_model: {edit.model}\n---"
    head = re.sub(r"(?m)^([ \t]*default_model:[ \t]+)\S+", lambda m: m[1] + edit.model, head)
    newline = "\r\n" if "\r\n" in text else "\n"
    body = edit.instructions.replace("\r\n", "\n").replace("\n", newline)
    result = (head + newline + body).encode("utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8")
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
        _check_engine(rt, agent.provider, edit.model)
        try:
            result = write_rules(Path(agent.role_path), edit, agent.model)
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from None
        agent.model = edit.model
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
