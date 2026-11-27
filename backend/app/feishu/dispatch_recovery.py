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

from sqlalchemy import select

from app.core.config import settings
from app.db.models import (Agent, AgentJob, AgentJobEvent, AgentJobInbox, Approval, DirectTurnReceipt, Event,
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


async def check_lead_model(db,cfg,thread,lead):
    from app.services import role_workspace
    who=cfg["steward"]
    binding=await db.get(FeishuLeadChat,thread.id)
    require(binding and binding.channel==signature(cfg["owner_id"],who["app_id"],who["tenant_key"],who["open_id"],
                                                   cfg.get("project",""),cfg["steward_thread"]))
    current=role_workspace.load(Path(lead.role_path),lead.model).model if lead.role_path else lead.model
    require(lead.provider==thread.provider and binding.selection==signature(lead.provider,current) and binding.model==current,
            "管家的模型已更换，请从当前会话重新确认。")


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
    await check_lead_model(db,cfg,thread,lead)
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


def original_reply(journal, cfg, root):
    """Find the registered reply target for the same steward, owner, tenant and original turn."""
    who=cfg.get("steward",{})
    if root.owner_id!=cfg.get("owner_id") or root.assignee_agent_id!=who.get("agent_id"):
        return None
    row=journal.execute("""select i.message from direct_team_reports r join inbox i using(app,event)
        join direct_turns d using(app,event) join owner_dm_sources s using(app,event,message)
        where r.job=? and i.app=? and d.thread=? and d.turn=? and s.owner=? and s.tenant=?""",
        (root.id,who.get("app_id"),root.requester_thread_id,root.requester_turn_id,who.get("open_id"),who.get("tenant_key"))).fetchone()
    return row[0] if row else None


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


async def followup_owner_request(db,root,thread):
    from app.team.feishu_assignment import configuration
    cfg=configuration()
    require(cfg and cfg.get("owner_id")==settings.STEWARD_STRICT_OWNER==root.owner_id==thread.owner_id
            and thread.agent_id==root.assignee_agent_id==cfg.get("steward",{}).get("agent_id"))
    lead=await db.get(Agent,root.assignee_agent_id)
    team=await service.team_for(db,root.owner_id)
    require(lead and not lead.archived_at and team and lead.id==team.lead_agent_id and lead.delegation_enabled)
    await check_lead_model(db,cfg,thread,lead)
    with channel(cfg) as journal:
        row,digest,original=source(journal,cfg,root.requester_turn_id,latest=False)
        require(original_reply(journal,cfg,root)==row["message"])
    receipt=await db.get(DirectTurnReceipt,(root.owner_id,event_key(row)))
    require(receipt and receipt.turn_id==root.requester_turn_id and receipt.thread_id==thread.id
            and receipt.input_sha256==digest)
    return original


async def bind_wake(db, root_id, thread, turn_id, *, internal):
    root=await db.get(AgentJob,root_id,with_for_update=True)
    require(internal and is_recovery(root) and root.status=="running" and root.current_turn_id is None
            and root.owner_id==thread.owner_id and root.assignee_thread_id==thread.id and root.assignee_agent_id==thread.agent_id)
    await checked_wake(db,root)
    root.assignee_turn_id=root.current_turn_id=turn_id


async def checked_child(db,root,proof):
    children=list(await db.scalars(select(AgentJob).where(AgentJob.parent_id==root.id)))
    require(len(children)==1,"管家未能为这次来信完成派工。")
    child=children[0]
    employee=await db.get(Agent,child.assignee_agent_id)
    require(child.owner_id==root.owner_id and child.root_id==root.id and child.kind=="job" and child.mode=="work" and child.reply=="resume"
            and child.origin_kind=="person" and child.requester_thread_id==root.requester_thread_id
            and child.requester_turn_id==root.requester_turn_id and child.requester_agent_id==root.requester_agent_id
            and child.assignee_agent_id==proof["employee"] and child.task_id==proof["task"]
            and child.assignment_epoch==proof["epoch"] and employee and employee.owner_id==root.owner_id
            and not employee.archived_at and await feature_tasks.assignment_current(db,child),
            "工单或经办人已变化，未发送原派工结果。")
    owner_source=await db.scalar(select(AgentJobEvent.detail).where(AgentJobEvent.job_id==child.id,AgentJobEvent.kind=="owner_request"))
    require(isinstance(owner_source,dict) and owner_source.get("turn_id")==root.requester_turn_id
            and owner_source.get("thread_id")==root.requester_thread_id and owner_source.get("event_key"))
    receipt=await db.get(DirectTurnReceipt,(root.owner_id,owner_source["event_key"]))
    require(receipt and (receipt.thread_id,receipt.turn_id)==(root.requester_thread_id,root.requester_turn_id))
    return child,employee


async def followup_scope(db, root, inbox_ids, *, require_pending=False):
    child,_=await checked_child(db,root,await proof_for(db,root))
    inbox=list(await db.scalars(select(AgentJobInbox).where(AgentJobInbox.id.in_(inbox_ids),
        AgentJobInbox.owner_id==root.owner_id,AgentJobInbox.holder_id==root.id,AgentJobInbox.job_id==child.id)))
    require(len(inbox)==len(inbox_ids) and inbox)
    question=any(item.kind=="needs_input" for item in inbox) and child.status=="needs_input" and bool(child.question)
    from app.team import steward_review
    approvals=[]
    for item in inbox:
        if item.kind.startswith("review:"):
            approval=await db.get(Approval,item.kind.partition(":")[2])
            if await steward_review.preview(db,root.owner_id,child,approval):
                approvals.append(approval.id)
    if require_pending:
        require(question or approvals,"员工问题或审批已处理。")
    return {"job":child.id,"turn":child.assignee_turn_id,"question":bool(question),"approvals":approvals}


def permits_followup(scope, tool, args):
    if not isinstance(args,dict) or args.get("job_id")!=scope["job"]:
        return False
    if tool=="mcp__team__answer_job_question":
        return scope["question"] and args.get("question_turn_id")==scope["turn"]
    return (tool in ("mcp__team__review_employee_write", "mcp__team__review_employee_action") and args.get("turn_id")==scope["turn"]
            and args.get("approval_id") in scope["approvals"])


async def parked_question(db, root, turn=None):
    if turn is not None:
        started=await db.scalar(select(Event.data).where(Event.turn_id==turn.id,Event.type=="turn.started"))
        if not (started or {}).get("recovery_followup"):
            return None
    child,_=await checked_child(db,root,await proof_for(db,root))
    if child.status=="needs_input" and child.question:
        from .task_label import task_label
        return f"老板，{await task_label(db, child)} 有个问题需要您确认。\n{child.question}\n您直接回复我就行。"
    return None


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
    from app.team import document_delivery
    proof=await proof_for(db,root)
    child,employee=await checked_child(db,root,proof)
    require(child.status=="completed","员工本次派工尚未成功交付。")
    needed,pin=await document_delivery.requirement(db,child)
    if proof.get("require_document"):
        needed=document_delivery.PUBLISH
    if not needed:
        require(child.result and child.result.strip(), "员工没有提交本次结果。")
        return child.result
    receipt=await document_delivery.evidence(db,child,needed,pin=pin)
    require(receipt.result,receipt.error)
    if needed in document_delivery.WRITES:
        return "老板，"+document_delivery.verified_line(child,employee.name,receipt)
    return child.result or "员工已取得本轮飞书工具回执。"


def main():
    from app.db.session import AsyncSessionLocal
    from app.team.feishu_assignment import configuration
    cli=argparse.ArgumentParser(description="核对并按原飞书来信续办一张被拒的派工单。")
    cli.add_argument("--turn",type=int,required=True)
    cli.add_argument("--task",type=int,required=True)
    cli.add_argument("--apply",action="store_true",help="显式登记一次续办。默认仅核对。")
    cli.add_argument("--require-published-document",action="store_true",help="要求经办员工本轮取得飞书知识库发布回执。")
    cli.add_argument("--retry-unpublished-turn",type=int,help="在同一员工会话续办已核实只读过来源的上一轮。")
    args=cli.parse_args()
    try:
        cfg=configuration()
        require(cfg and Path(cfg["_access_config_path"]).resolve().parent==settings.SQLITE_FILE.resolve().parent)
        if args.retry_unpublished_turn is not None:
            from .unpublished_retry import retry
            require(args.require_published_document)
            operation=retry(AsyncSessionLocal,cfg,args.turn,args.task,args.retry_unpublished_turn,apply=args.apply)
        else:
            operation=recover(AsyncSessionLocal,cfg,args.turn,args.task,apply=args.apply,require_document=args.require_published_document)
        print(json.dumps(asyncio.run(operation)))
    except RecoveryDenied as exc:
        cli.exit(2, str(exc)+"\n")
    except (ValueError,OSError,sqlite3.Error):
        cli.exit(2,"原来信、工单或授权条件不满足，未启动新的续办。\n")


if __name__=="__main__":
    # Recovery helpers import this module. Use the same exception type at the CLI boundary.
    from app.feishu.dispatch_recovery import main as run
    run()
