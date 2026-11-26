"""Turn-scoped access to the owner's explicitly configured employee Feishu resources."""
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import sqlite3

import httpx
from sqlalchemy import select

from app.db.models import AgentJob, Turn
from app.feishu import employee_tools, knowledge_document, owner_oauth, workspace_access
from app.feishu.publisher import PlatformError
from app.team import service


@dataclass
class Grant:
    job: AgentJob
    assignment: dict
    config: dict
    tools: tuple[str, ...]


def configured(cfg, employee, proof):
    identity = owner_oauth.employee_config(cfg, employee)
    status = owner_oauth.status(identity)
    actions = status["actions"]
    if not identity.get("_employee_oauth"):
        return None
    action = proof["action"]
    def doc_blocker():
        missing = set(owner_oauth.ACTIONS["doc"])-set(status.get("scopes",[]))
        staff=next((name for name,role in cfg.get("roles",{}).items() if role.get("agent_id")==employee),"该员工")
        detail="、".join(sorted(missing)) or "文档读写"
        return {**proof,"delivery_blocker":f"请在飞书开放平台核对并发布{staff}应用的用户权限 {detail}，然后到工作台 → {staff} → 技能与连接 → 飞书连接 → 授权文档读写。"}
    if action == "delete_document":
        if not proof.get("event_key") or not proof.get("target_url"):
            return None
        if not actions.get("delete"):
            missing=sorted(owner_oauth.ACTIONS["delete"]-set(status.get("scopes",[])))
            return {**proof,"delivery_blocker":"请为该员工授权删除文档和核对目标文件夹（"+"、".join(missing)+"）。"},()
        return proof, ("feishu_delete_document",)
    if action=="read_document":
        return proof, ("feishu_read_document",)
    if action=="edit_document":
        if not actions.get("doc"):
            return doc_blocker(),()
        if not actions.get("folder"):
            return {**proof,"delivery_blocker":"请为员工授权核对云盘文件夹（space:document:retrieve）。"},()
        return proof,("feishu_read_document","feishu_edit_document",)
    if action == "create_document":
        if not actions["doc"]:
            return doc_blocker(), ()
        knowledge_document.target(cfg, employee)
        return proof, ("feishu_create_document",)
    settings = workspace_access.settings_for(cfg)
    contacts = [alias for alias, source in settings["history_sources"].items()
                if source.get("agent_id") == employee and source.get("oauth_app") == identity["steward"]["app_id"]]
    alias = proof.get("contact")
    if action not in ("employee", "read", "update", "restructure") or alias not in contacts:
        return None
    if not actions["history"] or not settings["allow_codex_summary"]:
        return None
    tools = ["feishu_list_sources", "feishu_read_history", "feishu_read_document"]
    from .feishu_assignment import required_tool
    publish_requested = required_tool(proof) == "feishu_publish_knowledge"
    if publish_requested and not actions["doc"]:
        return doc_blocker(), tuple(tools)
    if action != "read" and not proof.get("read_only") and publish_requested and actions["doc"]:
        try:
            knowledge_document.target(cfg, employee, alias)
        except (KeyError, ValueError, OSError):
            pass
        else:
            tools.append("feishu_publish_knowledge")
    return proof, tuple(tools)


async def grant(db, owner, run, team):
    if not team or team.owner_id != owner or run.agent_id == team.lead_agent_id or run.ctx.approval_mode == "plan":
        return None
    turn = await db.get(Turn, run.turn_id)
    if not turn or turn.status != "running" or turn.thread_id != run.thread_id or turn.owner_token != run.owner_token:
        return None
    job = await db.scalar(select(AgentJob).where(
        AgentJob.owner_id == owner, AgentJob.assignee_turn_id == run.turn_id,
        AgentJob.assignee_thread_id == run.thread_id, AgentJob.assignee_agent_id == run.agent_id,
        AgentJob.requester_agent_id == team.lead_agent_id, AgentJob.status == "running",
        AgentJob.mode == "work", AgentJob.origin_kind == "person"))
    if not job:
        return None
    from app.team.feishu_assignment import configuration, for_job
    try:
        proof = await for_job(db, job.id)
        if not proof or proof.get("employee") != run.agent_id:
            return None
        cfg = configuration()
        if not cfg or owner != cfg["owner_id"] or team.lead_agent_id != cfg["steward"]["agent_id"]:
            return None
        scoped = configured(cfg, run.agent_id, proof)
    except (ValueError, OSError, KeyError, RuntimeError, sqlite3.Error):
        return None
    return Grant(job, scoped[0], cfg, scoped[1]) if scoped else None


async def record(rt, job_id, run, name, stage, **details):
    from app.team.jobs import event
    async with rt.session() as db:
        job = await db.get(AgentJob, job_id)
        event(db, job, stage, {"employee":run.agent_id, "tool":name, **details})
        await db.commit()


async def execute(rt, run, name, args):
    if name not in run.ctx.team.feishu_tools:
        raise service.TeamError("outside_feishu_assignment", "此员工当前任务没有该飞书工具。")
    async with rt.session() as db:
        team = await service.team_for(db, rt.owner_id)
        scope = await grant(db, rt.owner_id, run, team)
        if not scope or name not in scope.tools or team.id != run.ctx.team.team_id:
            raise service.TeamError("outside_feishu_assignment", "飞书授权或当前派工已经变更。")
        job_id = scope.job.id
    await record(rt, job_id, run, name, "feishu_tool_started")
    proof, cfg = scope.assignment, scope.config
    if name == "feishu_create_document":
        if not isinstance(args,dict) or set(args)-{"body"} or (args and proof.get("body")):
            await record(rt, job_id, run, name, "feishu_tool_failed", category="invalid_input")
            raise service.TeamError("invalid_input", "文档标题和正文由原派工提供。")
        args = {"title":proof.get("title"),"body":proof.get("body") or args.get("body")}
    journal_path = Path(cfg["_access_config_path"]).parent / cfg["state_dir"] / "channel.sqlite3"
    try:
        with closing(sqlite3.connect(journal_path.as_uri() + "?mode=rw", uri=True)) as journal, httpx.Client(timeout=20) as client:
            journal.row_factory = sqlite3.Row
            result = await employee_tools.execute(cfg, run.agent_id, name, args,
                owner_id=rt.owner_id, journal_db=journal, client=client, allowed_actions=scope.tools,
                contact=proof.get("contact"), invocation_id=f"job-{job_id}", assignment=proof)
    except ValueError as exc:
        await record(rt, job_id, run, name, "feishu_tool_failed", category="validation")
        raise service.TeamError("feishu_tool_failed", str(exc)) from None
    except (httpx.HTTPError, PlatformError, OSError, sqlite3.Error, RuntimeError) as exc:
        code,detail=platform_failure(exc)
        await record(rt,job_id,run,name,"feishu_tool_failed",category=type(exc).__name__,detail=detail)
        raise service.TeamError(code,detail) from None
    receipt = None
    action = proof["action"]
    if action=="read_document" and name=="feishu_read_document" and result.get("complete"):
        receipt={"url":result["url"]}
    elif name=="feishu_read_document" and result.get("complete"):
        await record(rt, job_id, run, name, "feishu_read_verified")
    if action in ("read","employee") and name == "feishu_read_history" and result.get("complete"):
        receipt = {"messages_read": result["total"]}
    if (action in ("update","restructure","employee") and name == "feishu_publish_knowledge" or
            action == "delete_document" and name == "feishu_delete_document" or
            action=="edit_document" and name=="feishu_edit_document" or
            action == "create_document" and name == "feishu_create_document") and result.get("confirmed"):
        receipt = {"deleted":True} if action=="delete_document" else {"url": result["url"]}
        if result.get("message"):
            receipt["message"] = result["message"]
        if "messages_read" in result:
            receipt["messages_read"] = result["messages_read"]
    if receipt is not None:
        from app.team.jobs import event
        async with rt.session() as db:
            job = await db.get(AgentJob, job_id)
            event(db, job, "feishu_delivered", {"employee":run.agent_id, "action":action,
                                                "result":receipt, "tool":name})
            await db.commit()
    return result

def platform_failure(exc):
    """Expose the failed step and safe platform error codes, never headers or private content."""
    if isinstance(exc,httpx.HTTPStatusError):
        response=exc.response
        try:
            data=response.json()
        except ValueError:
            data={}
        code=data.get("code") if isinstance(data,dict) else None
        detail=f"飞书请求失败（HTTP {response.status_code}"
        if isinstance(code,int):
            detail+=f"，飞书错误码 {code}"
        detail+="）。"
        if (code==99991679 and response.request.method=="GET" and
                "/docx/v1/documents/" in response.request.url.path):
            return "feishu_readback_denied",detail+"读取云文档时被拒，请核对员工应用的用户权限 docx:document:readonly。"
        return "feishu_tool_failed",detail
    if isinstance(exc,PlatformError):
        code=str(exc).removeprefix("Feishu error ")
        if code.isdecimal():
            return "feishu_tool_failed",f"飞书未确认本次操作（飞书错误码 {code}）。"
    return "feishu_tool_failed",f"飞书未确认本次操作（{type(exc).__name__}）。"
