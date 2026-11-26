"""Resource selection for the signed-in owner of the home management portal."""
import re
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app.feishu import workspace_access as access
from app.feishu.publisher import PlatformError
from app.routers.agents import get_runtime
from app.routers.feishu_owner_oauth import owner_browser
from app.services.thread_runtime import ThreadRuntime
from app.team.service import TeamError, member, team_for


router = APIRouter(prefix="/feishu", dependencies=[Depends(owner_browser)])


class Revision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class Folder(Revision):
    folder_url: str = Field(max_length=2048)


class Contact(Revision):
    alias: str = Field(min_length=1,max_length=40,pattern=r"^[\w·.\-]+$")
    chat: str = Field(min_length=1,max_length=2048)
    agent_id: int | None = Field(default=None,gt=0,strict=True)


class Analysis(Revision):
    enabled: bool = Field(strict=True)


def profile():
    try:
        return access.config_from_env()
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from None


def save(cfg, revision, **fields):
    try:
        return access.save_settings(cfg["_access_config_path"], expected_revision=revision, **fields)
    except access.SettingsChanged as exc:
        raise HTTPException(409,str(exc)) from None
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from None


def document_folder(url):
    if not url.strip():
        return {}
    parsed = urlsplit(url.strip())
    match = re.fullmatch(r"/drive/folder/([A-Za-z0-9]{1,128})/?", parsed.path)
    if not match or parsed.scheme != "https":
        raise ValueError("请粘贴飞书云盘文件夹的完整链接，路径中应包含 /drive/folder/。")
    return access.validate_document_target({"folder_token":match[1], "tenant_origin":"https://"+parsed.netloc})


def chat_reference(text):
    value = text.strip()
    if not re.fullmatch(r"oc_[A-Za-z0-9]{1,100}",value):
        url = urlsplit(value)
        if url.scheme != "https" or not re.fullmatch(r"[a-zA-Z0-9-]+\.feishu\.cn", url.netloc):
            raise ValueError("请粘贴带有会话 ID 的飞书链接，或以 oc_ 开头的会话 ID。")
        values = [item for key,items in parse_qs(url.query).items() if key.lower() in {"openchatid","chatid","chat_id"} for item in items]
        if len(values) == 1:
            value = values[0]
        if not re.fullmatch(r"oc_[a-zA-Z0-9]{1,100}",value):
            raise ValueError("链接里没有可识别的会话 ID，请使用以 oc_ 开头的 ID。")
    return value


@router.get("/access")
def list_access():
    return access.settings_for(profile())


@router.put("/document-target")
def set_folder(body: Folder):
    try:
        target = document_folder(body.folder_url)
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from None
    return save(profile(),body.revision,document_target=target)


@router.put("/analysis")
def set_analysis(body: Analysis):
    return save(profile(),body.revision,allow_codex_summary=body.enabled)


@router.post("/contacts")
async def set_contact(body: Contact, rt: ThreadRuntime = Depends(get_runtime)):
    from app.feishu.history import resolve_peer
    cfg = profile()
    current = access.settings_for(cfg)
    if body.revision != current["revision"]:
        raise HTTPException(409,"其他设备已改过飞书设置，请重新读取。")
    if body.agent_id is not None:
        async with rt.session() as db:
            team = await team_for(db, rt.owner_id)
            if not team:
                raise HTTPException(422,"请先在后台建立团队。")
            try:
                await member(db, rt.owner_id, body.agent_id, team_id=team.id)
            except TeamError:
                raise HTTPException(422,"请选择当前团队中在职的员工。") from None
    try:
        chat_id = chat_reference(body.chat)
        from app.feishu.owner_oauth import employee_config
        selected = employee_config(cfg, body.agent_id)
        peer = await run_in_threadpool(resolve_peer, selected, chat_id)
    except (httpx.HTTPError, PlatformError):
        raise HTTPException(502,"飞书无法核对这段单聊，请检查个人授权、应用权限和会话 ID。") from None
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from None
    sources = {**current["history_sources"], body.alias:{"chat_id":chat_id, "peer_open_id":peer, "agent_id":body.agent_id}}
    if selected.get('_employee_oauth'):
        sources[body.alias]['oauth_app'] = selected['steward']['app_id']
    return save(cfg,body.revision,history_sources=sources)


@router.delete("/contacts/{alias}")
def remove_contact(alias: str, body: Revision):
    cfg = profile()
    sources = dict(access.settings_for(cfg)["history_sources"])
    if alias not in sources:
        raise HTTPException(404,"没有找到这位联系人。")
    del sources[alias]
    return save(cfg,body.revision,history_sources=sources)
