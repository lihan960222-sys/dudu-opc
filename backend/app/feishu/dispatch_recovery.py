"""Explicit local recovery of an authenticated DM whose sole handoff failed before dispatch."""
import argparse
import asyncio
from contextlib import closing
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

from sqlalchemy import select

from app.core.config import settings
from app.db.models import (Agent, AgentJob, AgentJobEvent, Approval, DirectTurnReceipt, Event,
                           FeatureMessage, FeatureScope, FeishuLeadChat, Thread, Turn, WorkItem)
from app.db.transactions import begin_write
from app.feishu.lead_chat import signature
from app.feishu.submissions import event_key
from app.services import assignment_context, feature_tasks
from app.team import service


class RecoveryDenied(ValueError):
    """Public messages never contain application identities, tool arguments or customer text."""


TOOL = "mcp__team__delegate"
REASON = "任务编号不属于当前老板或项目。"
MARK = "feishu_dispatch_recovery"
PROMPT = "Harness message, not from the person. 系统续办。请按已核实的原来信继续原派工。"


def require(ok, message="原派工记录无法完整核对，恢复已停止。"):
    if not ok:
        raise RecoveryDenied(message)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def failed_call(events, approvals, task, employee):
    """Require a one-to-one match between recorded approvals, calls and refusals."""
    from collections import Counter
    started, completed = {}, {}
    for kind, data in events:
        call = data.get("call_id")
        require(isinstance(call, str) and call)
        if kind == "tool.started":
            args = data.get("input")
            require(data.get("name") == TOOL and isinstance(args, dict) and call not in started)
            require(args.get("agent_ids") == [employee] and args.get("task_id") in (None,task)
                    and args.get("mode","work") == "work" and args.get("assignment_intent") in (None,"continue"))
            started[call] = args
        else:
            require(kind == "tool.completed" and call in started and call not in completed and not data.get("truncated"))
            try:
                output = json.loads(data["output"])
            except (KeyError, TypeError, ValueError):
                raise RecoveryDenied("原派工缺少可核实的工具回执。") from None
            require(isinstance(output,dict) and set(output)=={"error","code","receipts"})
            code, error=output["code"],output["error"]
            require(code=="assignment_reference_scope" or (code=="assignment_context" and error==REASON))
            require(output["receipts"]==[{"agent_id":employee,"code":code,"error":error}])
            completed[call] = True
    require(started and started.keys() == completed.keys())
    require(all(a.tool == TOOL and a.status == "allowed" and a.consumed_at for a in approvals))
    require(Counter(fingerprint(a.input) for a in approvals) == Counter(fingerprint(v) for v in started.values()))
    exact = next((v for v in started.values() if v.get("task_id") == task), None)
    require(exact, "原派工没有明确指定这张工单。")
    return exact


def channel(cfg, *, writable=False):
    path = Path(cfg["_access_config_path"]).resolve(strict=True).parent / cfg["state_dir"] / "channel.sqlite3"
    db = sqlite3.connect(path.resolve(strict=True).as_uri()+("?mode=rw" if writable else "?mode=ro"), uri=True, timeout=3)
    db.row_factory = sqlite3.Row
    return closing(db)


def canonical_call(original, tool, arguments):
    """An engine selects the already approved operation; it cannot change its destination or contents."""
    if (tool!=TOOL or not isinstance(arguments,dict) or arguments.get("agent_ids")!=original["agent_ids"]
            or arguments.get("task_id")!=original["task_id"] or arguments.get("mode","work")!="work"):
        return None
    return original


def source(db, cfg, turn_id, *, latest=True):
    who = cfg["steward"]
    rows = db.execute("""select i.rowid as marker,i.app,i.event,i.message,i.state,i.payload,s.chat,d.thread,r.body
        from direct_turns d join inbox i using(app,event) join owner_dm_sources s using(app,event,message)
        join direct_requests r using(app,event) where i.app=? and d.turn=? and d.thread=r.thread
          and s.owner=? and s.tenant=? and i.task is null
          and not exists(select 1 from owner_readwrite_requests a where a.app=i.app and a.event=i.event)
          and not exists(select 1 from employee_feishu_assignments a where a.app=i.app and a.event=i.event)""",
        (who["app_id"],turn_id,who["open_id"],who["tenant_key"])).fetchall()
    require(len(rows) == 1)
    row = rows[0]
    message, body = json.loads(row["payload"]), json.loads(row["body"])
    require(row["state"]=="done" and message.get("chat_type")=="p2p" and message.get("message_type")=="text"
            and message.get("message_id")==row["message"] and message.get("chat_id")==row["chat"])
    original = json.loads(message["content"]).get("text")
    require(isinstance(original,str) and original.strip()==(body.get("owner_text") or body["prompt_text"]).strip()
            and body.get("direct_event_key")==event_key(row) and not body.get("attachment_ids"))
    require(db.execute("""select 1 from direct_finals f join outbox o on o.id=f.outgoing where f.app=? and f.event=?
        and o.state='sent' and o.role='steward' and json_extract(o.body,'$.reply_to')=?""",
        (row["app"],row["event"],row["message"])).fetchone(), "原回复尚未确认送达。")
    if latest:
        newer = db.execute("""select 1 from inbox i join owner_dm_sources s using(app,event,message)
            where i.rowid>? and i.app=? and s.owner=? and s.tenant=? and s.chat is ? limit 1""",
            (row["marker"],row["app"],who["open_id"],who["tenant_key"],row["chat"])).fetchone()
        require(not newer, "老板已发来更新的指令，不能恢复这条旧来信。")
    fields = {k:body[k] for k in ("owner_text","reply_to_turn_id","reference_event_key","reference_task_id") if body.get(k) is not None}
    digest = feature_tasks.direct_input_hash(body["prompt_text"],body.get("attachment_ids"),body.get("skills"),fields)
    return row, digest, original


@dataclass
class Evidence:
    employee: int
    epoch: int
    thread: int
    lead: int
    mode: str
    arguments: dict
    original: str


async def validate(db, cfg, turn_id, task, *, expected_epoch=None, root_id=None):
    owner, who = cfg["owner_id"], cfg["steward"]
    require(owner and settings.STEWARD_STRICT_OWNER==owner)
    with channel(cfg) as journal:
        row, digest, original = source(journal,cfg,turn_id)
        reporter=journal.execute("select job from direct_team_reports where app=? and event=?",(row["app"],row["event"])).fetchone()
        require(reporter is None or reporter[0]==root_id)
    receipt = await db.get(DirectTurnReceipt,(owner,event_key(row)))
    turn = await db.get(Turn,turn_id)
    thread = await db.get(Thread,row["thread"])
    lead = await db.get(Agent,who["agent_id"])
    team = await service.team_for(db,owner)
    require(receipt and turn and thread and team and lead and turn.status=="succeeded"
            and receipt.turn_id==turn_id and receipt.thread_id==thread.id==turn.thread_id and receipt.input_sha256==digest
            and thread.kind=="chat" and not thread.archived_at and not thread.job_id and not thread.room_id
            and team.lead_agent_id==thread.agent_id==lead.id and lead.owner_id==thread.owner_id==owner
            and not lead.archived_at and lead.delegation_enabled and lead.team_id==team.id
            and lead.provider==thread.provider)
    require(turn.finished_at and timedelta(0)<=service.now()-turn.finished_at<=timedelta(days=1),
            "原来信已过期，需要老板重新确认。")
    binding = await db.get(FeishuLeadChat,thread.id)
    require(binding and binding.channel==signature(owner,who["app_id"],who["tenant_key"],who["open_id"],
                                                 cfg.get("project",""),cfg["steward_thread"]))
    newer = await db.scalar(select(Event.id).where(Event.thread_id==thread.id, Event.turn_id>turn_id,
        Event.type=="turn.started", Event.data["internal"].as_boolean().is_not(True)).limit(1))
    require(not newer,"同一管家会话已有新的老板来信。")
    require(not await db.scalar(select(Event.id).where(Event.turn_id==turn_id,Event.type=="turn.steered").limit(1)))
    require(not await db.get(FeatureMessage,(owner,receipt.event_key)))
    context = await assignment_context.resolve_assignment_context(db,owner,turn_id)
    require(context.event_key==receipt.event_key and context.reference_task_ids==(task,) and context.original_text.strip()==original.strip())
    item, scope = await db.get(WorkItem,task), await db.get(FeatureScope,task)
    require(item and scope and item.owner_id==owner and not item.archived and not item.completed_at)
    employee = await db.get(Agent,item.agent_id)
    require(employee and employee.owner_id==owner and employee.team_id==team.id and not employee.archived_at
            and employee.manager_agent_id in (None,lead.id) and employee.id != lead.id and employee.role_path)
    require(not context.named_agent_ids or context.named_agent_ids==(employee.id,))
    require(expected_epoch is None or scope.assignment_epoch==expected_epoch, "工单已改派。")
    prior = await db.scalar(select(AgentJob.id).where(AgentJob.owner_id==owner, AgentJob.kind=="job",
        AgentJob.mode=="work",AgentJob.task_id==task,AgentJob.requester_agent_id==lead.id,
        AgentJob.requester_thread_id==thread.id,AgentJob.requester_turn_id<turn_id,
        AgentJob.assignee_agent_id==employee.id,AgentJob.assignment_epoch==scope.assignment_epoch,
        AgentJob.project==scope.project, AgentJob.created_at<=turn.started_at).limit(1))
    require(prior,"原来信的老板、工单和经办员工没有可核对的派工关系。")
    existing = select(AgentJob.id).where(AgentJob.owner_id==owner,
        ((AgentJob.requester_thread_id==thread.id) & (AgentJob.requester_turn_id==turn_id)) |
        ((AgentJob.task_id==task) & (AgentJob.kind=="job") & (AgentJob.created_at>=turn.started_at)))
    if root_id is not None:
        existing=existing.where(AgentJob.id!=root_id)
    require(not await db.scalar(existing.limit(1)),"原来信或工单已有后续派工。")
    events=(await db.execute(select(Event.type,Event.data).where(Event.turn_id==turn_id,
        Event.type.in_(("tool.started","tool.completed"))).order_by(Event.seq))).all()
    approvals=list(await db.scalars(select(Approval).where(Approval.turn_id==turn_id,Approval.thread_id==thread.id)))
    args=failed_call(events,approvals,task,employee.id)
    return Evidence(employee.id,scope.assignment_epoch,thread.id,lead.id,lead.approval_mode or "ask",args,original)


def is_recovery(job):
    return bool(job and job.kind=="root" and job.origin_ref==MARK)


async def proof_for(db, root):
    proof=await db.scalar(select(AgentJobEvent.detail).where(AgentJobEvent.job_id==root.id,AgentJobEvent.kind==MARK))
    require(is_recovery(root) and isinstance(proof, dict))
    return proof


async def recover(sessions, cfg, turn_id, task, *, apply=False, require_document=False):
    """Reserve in the team DB; release the root only after its original delivery is registered."""
    owner=cfg["owner_id"]
    async with sessions() as db:
        if apply:
            await begin_write(db)
            await service.team_for(db,owner,lock=True)
        root=await db.scalar(select(AgentJob).where(AgentJob.owner_id==owner,AgentJob.origin_ref==MARK,
            AgentJob.kind=="root",AgentJob.requester_turn_id==turn_id))
        if root:
            proof=await proof_for(db,root)
            with channel(cfg) as journal:
                row,digest,_=source(journal,cfg,turn_id,latest=False)
            receipt=await db.get(DirectTurnReceipt,(owner,event_key(row)))
            require(owner==settings.STEWARD_STRICT_OWNER and receipt and receipt.input_sha256==digest
                    and receipt.turn_id==turn_id and receipt.thread_id==root.requester_thread_id==row["thread"]
                    and proof["task"]==task and bool(proof.get("require_document"))==require_document)
            if root.status!="recovery_pending":
                return {"root":root.id,"state":root.status,"already_registered":True}
        info=await validate(db,cfg,turn_id,task,expected_epoch=proof["epoch"] if root else None,root_id=root.id if root else None)
        if not apply:
            return {"eligible":True,"task":task,"employee":info.employee}
        if root is None:
            root=AgentJob(owner_id=owner,kind="root",reply="none",status="recovery_pending",depth=0,
                origin_kind="person",origin_ref=MARK,requester_agent_id=info.lead,requester_thread_id=info.thread,
                requester_turn_id=turn_id,assignee_agent_id=info.lead,assignee_thread_id=info.thread,
                mode="work",brief="续办原派工",brief_hash="",approval_mode=info.mode,started_at=service.now(),
                budget_requests=settings.TEAM_MAX_JOBS_PER_ROOT,budget_executions=settings.TEAM_MAX_EXECUTIONS_PER_ROOT)
            db.add(root)
            await db.flush()
            db.add(AgentJobEvent(job_id=root.id,kind=MARK,detail={"task":task,"employee":info.employee,
                "epoch":info.epoch,"call":fingerprint(info.arguments),"require_document":require_document}))
        root_id=root.id
        await db.commit()
    with channel(cfg,writable=True) as journal, journal:
        row, _, _=source(journal,cfg,turn_id)
        journal.execute("insert or ignore into direct_team_reports(app,event,job) values(?,?,?)",(row["app"],row["event"],root_id))
        registered=journal.execute("select job from direct_team_reports where app=? and event=?",(row["app"],row["event"])).fetchone()
        require(registered and registered[0]==root_id)
    async with sessions() as db:
        await begin_write(db)
        root=await db.get(AgentJob,root_id,with_for_update=True)
        if root.status=="recovery_pending":
            root.status="resume"
            await db.commit()
        return {"root":root_id,"state":root.status}


async def checked_wake(db, root):
    from app.team.feishu_assignment import configuration
    proof=await proof_for(db,root)
    cfg=configuration()
    require(cfg and cfg["owner_id"]==root.owner_id)
    info=await validate(db,cfg,root.requester_turn_id,proof["task"],expected_epoch=proof["epoch"],root_id=root.id)
    require(info.employee==proof["employee"] and fingerprint(info.arguments)==proof["call"])
    return info


async def bind_wake(db, root_id, thread, turn_id, *, internal):
    root=await db.get(AgentJob,root_id,with_for_update=True)
    require(internal and is_recovery(root) and root.status=="running" and root.current_turn_id is None
            and root.owner_id==thread.owner_id and root.assignee_thread_id==thread.id and root.assignee_agent_id==thread.agent_id)
    await checked_wake(db,root)
    root.assignee_turn_id=root.current_turn_id=turn_id


async def guard_child(db, root, args, target, reply):
    proof=await proof_for(db,root)
    raw={k:v for k,v in args.items() if k!="agent_id"}
    raw["agent_ids"]=[target]
    require(reply=="resume" and target==proof["employee"] and fingerprint(raw)==proof["call"],
            "续办只可重复本次已批准的同一派工。")
    child=await db.scalar(select(AgentJob).where(AgentJob.parent_id==root.id))
    if child:
        require(child.task_id==proof["task"] and child.assignee_agent_id==target and child.assignment_epoch==proof["epoch"])
        return child
    await checked_wake(db,root)
    return None


async def confirmed_result(db, root):
    """The supervisor's answer cannot replace the employee's current completion or its required tool receipt."""
    from app.team.feishu_assignment import required_tool
    proof=await proof_for(db,root)
    children=list(await db.scalars(select(AgentJob).where(AgentJob.parent_id==root.id)))
    require(len(children)==1, "大嘴未能为这次来信完成派工。")
    child=children[0]
    require(child.owner_id==root.owner_id and child.kind=="job" and child.mode=="work" and child.reply=="resume"
            and child.origin_kind=="person" and child.requester_thread_id==root.requester_thread_id
            and child.requester_turn_id==root.requester_turn_id and child.requester_agent_id==root.requester_agent_id
            and child.assignee_agent_id==proof["employee"] and child.task_id==proof["task"]
            and child.assignment_epoch==proof["epoch"] and await feature_tasks.assignment_current(db,child),
            "工单或经办人已变化，未发送原派工结果。")
    require(child.status=="completed","员工本次派工尚未成功交付。")
    owner_source=await db.scalar(select(AgentJobEvent.detail).where(AgentJobEvent.job_id==child.id,AgentJobEvent.kind=="owner_request"))
    require(isinstance(owner_source,dict) and owner_source.get("turn_id")==root.requester_turn_id
            and owner_source.get("thread_id")==root.requester_thread_id and owner_source.get("event_key"))
    receipt=await db.get(DirectTurnReceipt,(root.owner_id,owner_source["event_key"]))
    require(receipt and (receipt.thread_id,receipt.turn_id)==(root.requester_thread_id,root.requester_turn_id))
    pin=await db.scalar(select(AgentJobEvent.detail).where(AgentJobEvent.job_id==child.id,AgentJobEvent.kind=="feishu_assignment"))
    needed="feishu_publish_knowledge" if proof.get("require_document") else required_tool(pin) if pin else None
    if not needed:
        require(child.result and child.result.strip(), "员工没有提交本次结果。")
        return child.result
    latest=await db.scalar(select(AgentJobEvent).where(AgentJobEvent.job_id==child.id,
        AgentJobEvent.detail["tool"].as_string()==needed,AgentJobEvent.kind.in_(("feishu_tool_started",
        "feishu_delivered","feishu_tool_failed","feishu_superseded"))).order_by(AgentJobEvent.id.desc()).limit(1))
    require(latest and latest.kind=="feishu_delivered" and latest.detail.get("employee")==child.assignee_agent_id
            and latest.detail.get("epoch")==child.assignment_epoch,"员工缺少本轮有效的飞书交付回执。")
    result=latest.detail.get("result") or {}
    if needed=="feishu_publish_knowledge":
        url, count=result.get("url"),result.get("messages_read")
        target=urlsplit(url) if isinstance(url,str) else None
        require(target and target.scheme=="https" and target.hostname and target.path.startswith("/docx/")
                and not target.query and not target.fragment and type(count) is int and count>=0,
                "本轮飞书发布回执缺少文档或来源数量。")
        later_failure=await db.scalar(select(AgentJobEvent.kind).where(AgentJobEvent.job_id==child.id,
            AgentJobEvent.id>latest.id,AgentJobEvent.detail["tool"].as_string()=="feishu_read_document",
            AgentJobEvent.kind.in_(("feishu_read_verified","feishu_delivered","feishu_superseded","feishu_tool_failed")))
            .order_by(AgentJobEvent.id.desc()).limit(1))
        require(later_failure not in ("feishu_superseded","feishu_tool_failed"),"后续飞书文档核对未通过。")
        return f"工单 #{child.task_id} 已由经办员工发布，飞书已确认文档，本次核对了 {count} 条来源消息。\n{url}"
    return child.result or "员工已取得本轮飞书工具回执。"


def main():
    from app.db.session import AsyncSessionLocal
    from app.team.feishu_assignment import configuration
    cli=argparse.ArgumentParser(description="核对并按原飞书来信续办一张被拒的派工单。")
    cli.add_argument("--turn",type=int,required=True)
    cli.add_argument("--task",type=int,required=True)
    cli.add_argument("--apply",action="store_true",help="显式登记一次续办。默认仅核对。")
    cli.add_argument("--require-published-document",action="store_true",help="要求经办员工本轮取得飞书知识库发布回执。")
    args=cli.parse_args()
    try:
        cfg=configuration()
        require(cfg and Path(cfg["_access_config_path"]).resolve().parent==settings.SQLITE_FILE.resolve().parent)
        print(json.dumps(asyncio.run(recover(AsyncSessionLocal,cfg,args.turn,args.task,apply=args.apply,
                                            require_document=args.require_published_document))))
    except (ValueError,OSError,sqlite3.Error):
        cli.exit(2,"原来信、工单或授权条件不满足，未启动新的续办。\n")


if __name__=="__main__":
    main()
