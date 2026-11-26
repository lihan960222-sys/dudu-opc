"""Verify delegated Feishu results from the host journal before reporting them to the owner."""
from dataclasses import dataclass
import hashlib
import re
import sqlite3
from urllib.parse import urlsplit

from sqlalchemy import select

from app.db.models import Agent, AgentJob, AgentJobEvent
from app.db.transactions import begin_write
from app.feishu.document_access import DOC_PATH, urls
from app.services.feature_tasks import assignment_current
from app.team import fence, service
from app.team.feishu_assignment import for_job, required_tool


PUBLISH = "feishu_publish_knowledge"
WRITES = (PUBLISH, "feishu_create_document", "feishu_edit_document", "feishu_delete_document")
STAGES = ("feishu_tool_started", "feishu_delivered", "feishu_superseded", "feishu_tool_failed")
MISSING = "我还没取得这次飞书交付的有效回执。"


@dataclass(frozen=True)
class Evidence:
    tool: str
    result: dict | None = None
    error: str = MISSING
    # An attempted or superseded operation is different from a worker asking for input before starting it.
    attempted: bool = False


async def requirement(db, job):
    pin = await db.scalar(select(AgentJobEvent.detail).where(
        AgentJobEvent.job_id==job.id, AgentJobEvent.kind=="feishu_assignment").order_by(AgentJobEvent.id).limit(1))
    if not pin:
        return None, {}
    tool = required_tool(pin)
    if tool in ("feishu_read_document","feishu_edit_document") and not pin.get("target_url"):
        links=urls(pin.get("original") or "")
        if len(links)==1:
            pin={**pin,"target_url":links[0]}
        elif pin.get("event_key"):
            try:
                verified=await for_job(db,job.id)
            except (ValueError,OSError,KeyError,sqlite3.Error):
                verified=None
            if verified and verified.get("target_url"):
                pin={**pin,"target_url":verified["target_url"]}
    if not tool and pin.get("action")=="employee":
        tool=await db.scalar(select(AgentJobEvent.detail["tool"].as_string()).where(
            AgentJobEvent.job_id==job.id,AgentJobEvent.kind=="feishu_delivery_expected").limit(1))
    if not tool and pin.get("action")=="employee":
        # Historical approved work could select a write while executing, without a separate delivery field.
        tool = await db.scalar(select(AgentJobEvent.detail["tool"].as_string()).where(
            AgentJobEvent.job_id==job.id, AgentJobEvent.kind.in_(STAGES),
            AgentJobEvent.detail["tool"].as_string().in_(WRITES)).order_by(AgentJobEvent.id.desc()).limit(1))
    return tool, pin


def document_url(value, *, wiki=False):
    if not isinstance(value,str):
        return False
    try:
        target = urlsplit(value)
        return bool(target.scheme=="https" and target.hostname and target.hostname.endswith((".feishu.cn",".larksuite.com"))
                    and target.port in (None,443) and target.username is None and
                    target.password is None and not target.fragment and (wiki or not target.query) and
                    (DOC_PATH.fullmatch(target.path) or wiki and re.fullmatch(r"/wiki/[A-Za-z0-9]{1,128}/?",target.path)) and
                    not any(c.isspace() for c in value))
    except ValueError:
        return False


def target_hash(url):
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


async def evidence(db, job, tool, *, pin=None, expected_url=None):
    """Check this job's last attempt, its assignee/turn/epoch and any subsequent readback."""
    pin = pin or {}
    if pin.get("employee",job.assignee_agent_id)!=job.assignee_agent_id or not await assignment_current(db,job):
        return Evidence(tool,error="派工已经变更，原交付回执不能用于这次汇报。",attempted=True)
    event = await db.scalar(select(AgentJobEvent).where(AgentJobEvent.job_id==job.id,
        AgentJobEvent.kind.in_(STAGES), AgentJobEvent.detail["tool"].as_string()==tool)
        .order_by(AgentJobEvent.id.desc()).limit(1))
    if event is None:
        return Evidence(tool)
    stamp = event.detail
    invalid = Evidence(tool,attempted=True)
    if event.kind!="feishu_delivered":
        detail=stamp.get("detail") if event.kind=="feishu_tool_failed" else None
        return Evidence(tool,error=detail or MISSING,attempted=True)
    if (stamp.get("employee")!=job.assignee_agent_id or stamp.get("epoch")!=job.assignment_epoch
            or stamp.get("turn_id",job.assignee_turn_id)!=job.assignee_turn_id):
        return invalid
    if "turn_id" not in stamp:
        # Older host receipts checked the active turn on insertion but did not store its ID.
        # They remain usable until the same job takes another execution.
        restarted = await db.scalar(select(AgentJobEvent.id).where(AgentJobEvent.job_id==job.id,
            AgentJobEvent.id>event.id, AgentJobEvent.kind.in_(("clarified","requeued","resumed","started"))).limit(1))
        if restarted:
            return invalid
    result = stamp.get("result")
    if not isinstance(result,dict):
        return invalid
    url = result.get("url")
    target = expected_url or pin.get("target_url")
    if pin.get("action") in ("edit_document","read_document") and not target:
        return invalid
    if target and tool!="feishu_delete_document" and url!=target:
        return invalid
    if tool in (PUBLISH,"feishu_create_document","feishu_edit_document"):
        if not document_url(url,wiki=tool=="feishu_edit_document"):
            return invalid
        # A later unsuccessful or unfinished read of the result withdraws the previous confirmation.
        checks = await db.scalars(select(AgentJobEvent).where(AgentJobEvent.job_id==job.id,
            AgentJobEvent.id>event.id, AgentJobEvent.detail["tool"].as_string()=="feishu_read_document",
            AgentJobEvent.kind.in_((*STAGES,"feishu_read_verified"))).order_by(AgentJobEvent.id.desc()))
        for check in checks:
            seen=check.detail.get("target_hash")
            if seen and seen!=target_hash(url):
                continue
            if check.kind in ("feishu_read_verified","feishu_delivered"):
                if (seen==target_hash(url) or (check.detail.get("result") or {}).get("url")==url) and (
                        check.detail.get("employee")==job.assignee_agent_id and check.detail.get("epoch")==job.assignment_epoch
                        and check.detail.get("turn_id",job.assignee_turn_id)==job.assignee_turn_id):
                    break
                continue
            return Evidence(tool,error=check.detail.get("detail") or "后续飞书文档核对尚未通过。",attempted=True)
    if tool=="feishu_delete_document" and result.get("deleted") is not True:
        return invalid
    if tool in ("feishu_read_history",PUBLISH) and (type(result.get("messages_read")) is not int or result["messages_read"]<0):
        return invalid
    if tool=="feishu_read_document" and (not isinstance(url,str) or not url):
        return invalid
    return Evidence(tool,result=result,error="",attempted=True)


def verified_line(job, name, receipt):
    label = f"{name}已按工单 #{job.task_id} " if job.task_id else f"{name}已"
    if receipt.tool==PUBLISH:
        return f"{label}核对了知识库，本轮读取了 {receipt.result['messages_read']} 条来源消息。\n{receipt.result['url']}"
    if receipt.tool=="feishu_delete_document":
        return f"{label}确认文档已移入回收站。"
    if receipt.tool in ("feishu_create_document","feishu_edit_document"):
        verb="创建" if receipt.tool=="feishu_create_document" else "修改"
        return f"{label}{verb}文档，并核对了正文和位置。\n{receipt.result['url']}"
    return f"{label}读完指定材料，读取回执已核对。"


async def children_with_contracts(db, root):
    if root.kind!="root" or root.origin_kind!="person":
        return []
    children = await db.scalars(select(AgentJob).where(AgentJob.owner_id==root.owner_id,
        AgentJob.parent_id==root.id, AgentJob.root_id==root.id, AgentJob.requester_agent_id==root.assignee_agent_id,
        AgentJob.requester_thread_id==root.assignee_thread_id, AgentJob.kind=="job", AgentJob.reply=="resume")
        .order_by(AgentJob.id))
    entries,latest = [],{}
    for child in children:
        tool, pin = await requirement(db,child)
        # An explicit retry replaces only the same employee, card/thread and agreed deliverable.
        key=(child.assignee_agent_id,child.task_id or ("thread",child.assignee_thread_id),tool,pin.get("contact"),
             pin.get("target_url"),pin.get("event_key"))
        if child.rework and key in latest:
            entries[latest[key]]=None
        latest[key]=len(entries)
        entries.append((child,tool,pin))
    return [entry for entry in entries if entry is not None]


@dataclass(frozen=True)
class Report:
    text: str
    failed: bool = False
    pending: bool = False


async def routing_errors(db, root):
    events=await db.scalars(select(AgentJobEvent.detail).where(AgentJobEvent.job_id==root.id,
        AgentJobEvent.kind=="feishu_delivery_routing").order_by(AgentJobEvent.id))
    errors={}
    for detail in events:
        key=(detail["employee"],detail["tool"])
        if detail["error"]:
            errors[key]=detail["error"]
        else:
            errors.pop(key,None)
    return errors


async def remember_routing(runtime,run,accepted):
    """Carry unmet parts of one owner request across supervisor restarts and mixed fan-out."""
    async with runtime.session() as db:
        await begin_write(db)
        root=await db.scalar(select(AgentJob).where(AgentJob.owner_id==runtime.owner_id,
            AgentJob.kind=="root",AgentJob.assignee_thread_id==run.thread_id,
            AgentJob.assignee_agent_id==run.agent_id,
            AgentJob.id==run.delivery_root if run.delivery_root else AgentJob.current_turn_id==run.turn_id)
            .with_for_update())
        previous=await routing_errors(db,root) if root else {}
        pending={**previous,**run.delivery_errors}
        for job in accepted:
            if root and (job.root_id!=root.id or job.parent_id!=root.id):
                continue
            tool,_=await requirement(db,job)
            pending.pop((job.assignee_agent_id,"choice"),None)
            if tool:
                pending.pop((job.assignee_agent_id,tool),None)
        run.delivery_errors=pending
        if not root:
            return
        for key in sorted(previous.keys()|pending.keys()):
            if previous.get(key)!=pending.get(key):
                db.add(AgentJobEvent(job_id=root.id,kind="feishu_delivery_routing",
                    detail={"employee":key[0],"tool":key[1],"error":pending.get(key),"turn":run.turn_id}))
        if pending:
            run.delivery_root=root.id
        await db.commit()


async def owner_report(db, root, draft, *, extra_errors=None):
    children = await children_with_contracts(db,root)
    errors={**await routing_errors(db,root),**(extra_errors or {})}
    if not any(tool for _,tool,_ in children) and not errors:
        return None
    lead = await db.get(Agent,root.assignee_agent_id)
    team=await service.team_for(db,root.owner_id)
    if not team or not lead or lead.archived_at or not (team.lead_agent_id==lead.id==root.requester_agent_id):
        return Report("老板，经办团队已经变更，我还不能确认原派工的交付。",True)
    failed, needs_receipt_text, lines = bool(errors), False, list(dict.fromkeys(errors.values()))
    pending=False
    for child,tool,pin in children:
        employee = await db.get(Agent,child.assignee_agent_id)
        if (not employee or employee.archived_at or
                employee.owner_id!=root.owner_id or employee.team_id!=team.id or
                not await assignment_current(db,child,lock=True)):
            lines.append("派工或经办人已经变更，我还不能确认交付。")
            failed=True
            continue
        name=fence.line(employee.name)
        if child.status not in ("completed","failed","cancelled"):
            pending=True
            needs_receipt_text=True
            if child.status=="needs_input" and child.question:
                lines.append(f"{name}需要您确认一件事。\n{fence.text(child.question,1000)}")
            else:
                lines.append(f"我已把任务交给{name}，还在等这次的交付回执。")
            continue
        receipt=await evidence(db,child,tool,pin=pin) if tool else None
        if child.status!="completed":
            failed=True
            detail=receipt.error if receipt and not receipt.result else ""
            lines.append(f"{name}这次没有完成交付。"+detail)
            if receipt and receipt.result and tool in WRITES:
                lines.append("此前已取得的飞书操作回执如下。"+verified_line(child,name,receipt))
            continue
        if receipt and receipt.result is None:
            failed=True
            lines.append(f"{name}这次的交付我还没核实。"+receipt.error)
        elif receipt and tool in WRITES:
            needs_receipt_text=True
            lines.append(verified_line(child,name,receipt))
        elif child.result:
            # Mixed work keeps the other employees' findings with their attribution.
            lines.append(f"{name}的回报：{fence.text(child.result,2000)}")
    if failed or needs_receipt_text:
        return Report("老板，"+"\n\n".join(lines),failed,pending)
    return Report(draft or "")
