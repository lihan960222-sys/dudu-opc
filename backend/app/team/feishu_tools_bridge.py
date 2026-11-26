"""Turn-scoped access to the owner's explicitly configured employee Feishu resources."""
from contextlib import closing
from dataclasses import dataclass
import hashlib
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


async def invocation_key(db, job, proof):
    """Use one key for retries of the same owner request, including a second lead turn."""
    from app.db.models import AgentJobEvent
    key = proof.get("event_key")
    if not key and job.task_id:
        owner_request = await db.scalar(select(AgentJobEvent.detail).where(
            AgentJobEvent.job_id==job.id,AgentJobEvent.kind=="owner_request").order_by(AgentJobEvent.id).limit(1))
        key = owner_request.get("event_key") if isinstance(owner_request,dict) else None
    if not isinstance(key,str) or not key or not job.task_id:
        return f"job-{job.id}"
    digest=hashlib.sha256(f"{job.owner_id}/{job.task_id}/{key}".encode()).hexdigest()
    return "operation-"+digest


def configured(cfg, employee, proof):
    if proof.get("employee", employee) != employee:
        return None
    identity = owner_oauth.employee_config(cfg, employee)
    status = owner_oauth.status(identity)
    actions = status["actions"]
    if not identity.get("_employee_oauth"):
        return None
    action = proof["action"]
    direct_read = ("feishu_read_document",) if proof.get("target_url") and (
        actions.get("doc") or "docx:document:readonly" in status.get("scopes", ())) else ()
    folder_blocker = {**proof, "delivery_blocker":"请为员工授权核对云盘文件夹（space:document:retrieve）。"}
    def doc_blocker():
        missing = set(owner_oauth.ACTIONS["doc"])-set(status.get("scopes",[]))
        staff=next((name for name,role in cfg.get("roles",{}).items() if role.get("agent_id")==employee),"该员工")
        detail="、".join(sorted(missing)) or "文档读写"
        return {**proof,"delivery_blocker":f"请在飞书开放平台核对并发布{staff}应用的用户权限 {detail}，然后到工作台 → {staff} → 技能与连接 → 飞书连接 → 授权文档读写。"}
    read_only = proof.get("read_only") or action in ("read", "read_document") or proof.get("expected_tool") == "feishu_read_history"
    if read_only and action in ("edit_document", "delete_document", "create_document"):
        return proof, direct_read
    if action == "delete_document":
        if not proof.get("event_key") or not proof.get("target_url"):
            return None
        if not actions.get("delete"):
            missing=sorted(owner_oauth.ACTIONS["delete"]-set(status.get("scopes",[])))
            return {**proof,"delivery_blocker":"请为该员工授权删除文档和核对目标文件夹（"+"、".join(missing)+"）。"},direct_read
        return proof, (*direct_read,"feishu_delete_document")
    if action=="read_document":
        return proof, ("feishu_read_document",)
    if action=="edit_document":
        if not actions.get("doc"):
            return doc_blocker(),direct_read
        if not actions.get("folder"):
            return folder_blocker,direct_read
        return proof,("feishu_read_document","feishu_edit_document",)
    if action == "create_document":
        tools=[]
        if proof.get("contact") and actions.get("history"):
            tools.extend(("feishu_list_sources","feishu_read_history","feishu_read_message","feishu_read_document"))
        if not actions["doc"]:
            return doc_blocker(), tuple(tools)
        if not actions.get("folder"):
            return folder_blocker, tuple(tools)
        knowledge_document.target(cfg, employee)
        return proof, (*tools,"feishu_create_document")
    settings = workspace_access.settings_for(cfg)
    contacts = [alias for alias, source in settings["history_sources"].items()
                if source.get("agent_id") == employee and source.get("oauth_app") == identity["steward"]["app_id"]]
    alias = proof.get("contact")
    if action not in ("employee", "read", "update", "restructure") or alias not in contacts:
        return None
    if not actions["history"] or not (settings["allow_codex_summary"] or settings.get("employee_summary_vendors", {}).get(str(employee))):
        return None
    tools = ["feishu_list_sources", "feishu_read_history", "feishu_read_message", "feishu_read_document"]
    from .feishu_assignment import required_tool
    publish_requested = required_tool(proof) == "feishu_publish_knowledge"
    if publish_requested and not actions["doc"]:
        return doc_blocker(), tuple(tools)
    if publish_requested and not read_only and not actions.get("folder"):
        return folder_blocker, tuple(tools)
    if not read_only and actions["doc"] and actions.get("folder"):
        try:
            knowledge_document.target(cfg, employee, alias)
        except (KeyError, ValueError, OSError):
            if publish_requested:
                return {**proof,"delivery_blocker":"请核对该员工知识库登记的联系人、文档名称和目标文件夹。"},tuple(tools)
        else:
            tools.append("feishu_publish_knowledge")
    return proof, tuple(tools)


async def grant(db, owner, run, team):
    if not team or team.owner_id != owner or run.agent_id == team.lead_agent_id:
        return None
    turn = await db.get(Turn, run.turn_id)
    if not turn or turn.status != "running" or turn.thread_id != run.thread_id or turn.owner_token != run.owner_token:
        return None
    job = await db.scalar(select(AgentJob).where(
        AgentJob.owner_id == owner, AgentJob.assignee_turn_id == run.turn_id,
        AgentJob.assignee_thread_id == run.thread_id, AgentJob.assignee_agent_id == run.agent_id,
        AgentJob.requester_agent_id == team.lead_agent_id, AgentJob.status == "running",
        AgentJob.mode == "work", AgentJob.origin_kind.in_(("person", "routine"))))
    if not job:
        return None
    from app.services.feature_tasks import assignment_current
    if not await assignment_current(db, job):
        return None
    from app.team.feishu_assignment import configuration, for_job
    try:
        cfg = configuration()
        if not cfg or owner != cfg["owner_id"] or team.lead_agent_id != cfg["steward"]["agent_id"]:
            return None
        if job.origin_kind == "routine":
            from app.feishu.weekly_knowledge import proof_for_job, TOOLS
            if not getattr(run.ctx, "scheduled_knowledge", False) or turn.origin != "routine":
                return None
            proof = await proof_for_job(db, cfg, job)
            if not proof or proof["weekly"]["vendor"] != getattr(run.ctx, "model_vendor", None):
                return None
            proof["runtime_vendor"] = run.ctx.model_vendor
            choice = configured(cfg, run.agent_id, proof)
            if not choice:
                return None
            return Grant(job, choice[0], cfg, tuple(name for name in choice[1] if name in TOOLS))
        proof = await for_job(db, job.id)
        if not proof or proof.get("employee") != run.agent_id:
            return None
        from app.feishu import unpublished_retry
        if (job.origin_ref or "").startswith(unpublished_retry.KEY_PREFIX):
            employee = await service.member(db, owner, run.agent_id, team_id=team.id)
            proof=await unpublished_retry.tool_proof(db,cfg,employee,job,proof,getattr(run.ctx,"model_vendor",None))
            if not proof:
                return None
        proof = {**proof, "runtime_vendor": getattr(run.ctx, "model_vendor", None) or "unconfirmed"}
        if run.ctx.approval_mode == "plan":
            proof["read_only"] = True
        scoped = configured(cfg, run.agent_id, proof)
    except (ValueError, OSError, KeyError, RuntimeError, sqlite3.Error):
        return None
    return Grant(job, scoped[0], cfg, scoped[1]) if scoped else None


async def _lock_receipt_job(db, owner, job_id, employee):
    from app.db.transactions import begin_write
    # A grant can be checked before another session updates the dispatch. On
    # SQLite, upgrading that earlier read snapshot would lose the receipt.
    await begin_write(db)
    job = await db.get(AgentJob, job_id, with_for_update=True, populate_existing=True)
    if not job or job.owner_id != owner or job.assignee_agent_id != employee:
        raise service.TeamError("outside_feishu_assignment", "飞书操作与派工身份不符。")
    return job


async def record(rt, job_id, run, name, stage, **details):
    from app.team.jobs import event
    async with rt.session() as db:
        job = await _lock_receipt_job(db, rt.owner_id, job_id, run.agent_id)
        event(db, job, stage, {"employee":run.agent_id, "tool":name, "epoch":job.assignment_epoch,
                               "turn_id":run.turn_id, **details})
        await db.commit()


async def record_delivery(rt, job_id, run, name, logical_tool, action, operation, receipt):
    from app.services.feature_tasks import assignment_current
    from app.team.jobs import event
    async with rt.session() as db:
        job = await _lock_receipt_job(db, rt.owner_id, job_id, run.agent_id)
        current = (job.status == "running" and job.assignee_turn_id == run.turn_id
                   and await assignment_current(db, job, lock=True))
        stage = "feishu_delivered" if current else "feishu_superseded"
        event(db, job, stage, {"employee":run.agent_id,"action":action,"result":receipt,
            "tool":logical_tool,"invoked_tool":name,"operation":operation,"epoch":job.assignment_epoch,
            "turn_id":run.turn_id})
        await db.commit()
    if not current:
        raise service.TeamError("assignment_replaced","派工已换人，刚才的操作结果已留给管家核对。")


async def execute(rt, run, name, args):
    from app.team import feishu_handoff
    if name not in run.ctx.team.feishu_tools:
        raise service.TeamError("outside_feishu_assignment", "此员工当前任务没有该飞书工具。")
    async with rt.session() as db:
        team = await service.team_for(db, rt.owner_id)
        scope = await grant(db, rt.owner_id, run, team)
        if not scope or name not in scope.tools or team.id != run.ctx.team.team_id:
            raise service.TeamError("outside_feishu_assignment", "飞书授权或当前派工已经变更。")
        job_id = scope.job.id
        operation = await invocation_key(db, scope.job, scope.assignment)
        scoped_write = name in feishu_handoff.WRITE_TOOLS and scope.job.task_id is not None
        if scoped_write:
            scope = await feishu_handoff.reserve(db,rt,run,scope,name,operation)
    proof, cfg = scope.assignment, scope.config
    read_target={}
    if name=="feishu_read_document" and isinstance(args,dict):
        from app.team.document_delivery import target_hash
        url=proof.get("target_url") or args.get("url")
        if isinstance(url,str) and url:
            read_target={"target_hash":target_hash(url)}
    if not scoped_write:
        await record(rt, job_id, run, name, "feishu_tool_started", **read_target)
    if proof.get("weekly"):
        async def weekly_live():
            async with rt.session() as db:
                team = await service.team_for(db, rt.owner_id)
                return bool(await grant(db, rt.owner_id, run, team))
        proof = {**proof, "_weekly_authorized":weekly_live}
    if proof.get("_retry_resource"):
        async def retry_live():
            async with rt.session() as db:
                team=await service.team_for(db,rt.owner_id)
                current=await grant(db,rt.owner_id,run,team)
                return bool(current and current.job.id==job_id and name in current.tools
                            and current.assignment.get("_retry_resource")==proof["_retry_resource"]
                            and current.assignment.get("_retry_document")==proof["_retry_document"])
        proof={**proof,"_retry_authorized":retry_live}
    if name == "feishu_create_document":
        if (not isinstance(args,dict) or set(args)-{"title","body"}
                or "body" in args and proof.get("body")
                or "title" in args and proof.get("title") and args["title"]!=proof["title"]):
            await record(rt, job_id, run, name, "feishu_tool_failed", category="invalid_input")
            raise service.TeamError("invalid_input", "文档标题和正文由原派工提供。")
        args = {"title":proof.get("title") or args.get("title"),"body":proof.get("body") or args.get("body")}
    journal_path = Path(cfg["_access_config_path"]).parent / cfg["state_dir"] / "channel.sqlite3"
    try:
        with closing(sqlite3.connect(journal_path.as_uri() + "?mode=rw", uri=True)) as journal, httpx.Client(timeout=20) as client:
            journal.row_factory = sqlite3.Row
            result = await employee_tools.execute(cfg, run.agent_id, name, args,
                owner_id=rt.owner_id, journal_db=journal, client=client, allowed_actions=scope.tools,
                contact=proof.get("contact"), invocation_id=operation, assignment=proof)
    except ValueError as exc:
        await record(rt, job_id, run, name, "feishu_tool_failed", category="validation", **read_target)
        raise service.TeamError("feishu_tool_failed", str(exc)) from None
    except (httpx.HTTPError, PlatformError, OSError, sqlite3.Error, RuntimeError) as exc:
        code,detail=platform_failure(exc)
        await record(rt,job_id,run,name,"feishu_tool_failed",category=type(exc).__name__,detail=detail, **read_target)
        raise service.TeamError(code,detail) from None
    receipt = None
    action = proof["action"]
    if action=="read_document" and name=="feishu_read_document" and result.get("complete"):
        receipt={"url":result["url"]}
    elif name=="feishu_read_document" and result.get("complete"):
        await record(rt, job_id, run, name, "feishu_read_verified", **read_target)
    logical_tool=name
    if action in ("read","employee") and name in ("feishu_read_history","feishu_read_message") and result.get("source_complete"):
        receipt = {"messages_read": result["total"]}
        logical_tool="feishu_read_history"
    if (action in ("update","restructure","employee") and name == "feishu_publish_knowledge" or
            action == "delete_document" and name == "feishu_delete_document" or
            action=="edit_document" and name=="feishu_edit_document" or
            action == "create_document" and name == "feishu_create_document") and result.get("confirmed"):
        receipt = {"deleted":True} if action=="delete_document" else {"url": result["url"]}
        if result.get("message"):
            receipt["message"] = result["message"]
        if "messages_read" in result:
            receipt["messages_read"] = result["messages_read"]
        if name=="feishu_publish_knowledge" and result.get("verification"):
            receipt["verification"]={key:result["verification"][key] for key in (
                "document_resolution","parent_verified","body_verified","source_messages_verified","same_title_entries")}
    if receipt is not None:
        await record_delivery(rt,job_id,run,name,logical_tool,action,operation,receipt)
    return result

def platform_failure(exc):
    """Expose the failed step and safe platform error codes, never headers or private content."""
    if isinstance(exc,owner_oauth.OwnerOAuthError):
        return "feishu_authorization_required",str(exc)
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
