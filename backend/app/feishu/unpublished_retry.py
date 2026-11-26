"""Continue a verified, undelivered knowledge document on the employee's original work order."""
from datetime import timedelta
import json
from pathlib import Path

from sqlalchemy import select

from app.core.config import settings
from app.db.models import AgentJob, AgentJobEvent, AgentJobInbox, Approval, DirectTurnReceipt, Event, FeatureMessage, Turn, WorkItem
from app.db.transactions import begin_write
from app.feishu import dispatch_recovery as recovery
from app.services import feature_tasks
from app.team import feishu_assignment, service
from app.team.feishu_handoff import WRITE_TOOLS

READ_TOOLS = {"employee_capabilities", "feishu_list_sources", "feishu_read_history", "feishu_read_message", "feishu_read_document"}
READ_TOOL_NAMES = {"mcp__team__" + name for name in READ_TOOLS}
RETRY_EVENT = "unpublished_delivery_retry"
STAGED = "delivery_retry_pending"
KEY_PREFIX = "cloud-delivery-"
NOTE = ("上次已读取来源，本轮继续原老板要求的云文档交付。请核对本岗位当前已授权的目标文件夹和工具。"
        "一个工具可以完成多步操作。按它实际支持的效果判断能否重建、写入、读回和返回链接。"
        "沿用已有整理稿和原消息来源，核对当前目标。同名或写入结果不明时先查清，完成后以本轮发布回执汇报。")


def only_reads(events):
    """A declined native command did not run. A nonzero command exit alone cannot prove that."""
    commands = set()
    for kind, data in events:
        name, call = data.get("name"), data.get("call_id")
        if kind == "tool.started":
            if name == "Bash" and call and call not in commands:
                commands.add(call)
            elif name not in READ_TOOL_NAMES:
                return False
        elif kind == "tool.completed" and call in commands:
            receipt = data.get("command_receipt") or {}
            if receipt.get("status") != "declined" or receipt.get("exit_code") is not None or data.get("ok") is not False:
                return False
            commands.remove(call)
    return not commands


def resource_scope(cfg, employee, pin):
    from app.feishu import knowledge_document, owner_oauth, workspace_access
    from app.services.engine_registry import resolve_model_profile
    from app.services.role_workspace import load
    alias = pin.get("contact")
    auth = owner_oauth.employee_config(cfg, employee.id)
    policy = workspace_access.settings_for(cfg)
    source = policy["history_sources"].get(alias)
    model = load(Path(employee.role_path), employee.model).model if employee.role_path else employee.model
    vendor = resolve_model_profile(employee.provider, employee.model_vendor, model).model_vendor
    recovery.require(auth.get("_employee_oauth") and source and source.get("agent_id") == employee.id
        and source.get("oauth_app") == auth["steward"]["app_id"]
        and source.get("chat_id") and source.get("peer_open_id")
        and workspace_access.permits_model(policy,(employee.id,None,None,None),{"runtime_vendor":vendor}))
    actions = owner_oauth.status(auth)["actions"]
    recovery.require(all(actions.get(k) for k in ("history","doc","folder")), "员工当前缺少本轮文档交付需要的授权。")
    dest,title = knowledge_document.target(cfg,employee.id,alias)
    recovery.require(not policy.get("document_target") or policy["document_target"] == dest)
    return outbound_scope(cfg,employee.id,alias,source,dest,title,auth,vendor)


def outbound_scope(cfg, employee, alias, source, dest, title, identity, vendor):
    from app.feishu import knowledge_document
    recovery.require(source and identity.get("_employee_oauth") and source.get("agent_id")==employee
                     and source.get("oauth_app")==identity["steward"]["app_id"])
    channel_path=(Path(cfg["_access_config_path"]).parent/cfg["state_dir"]/"channel.sqlite3").resolve()
    return {"binding":knowledge_document.binding_key(cfg["steward"]["app_id"],employee,alias,source,dest,title),
        "app":identity["steward"]["app_id"],"actor":identity["steward"]["open_id"],"chat":source["chat_id"],
        "tenant":cfg["steward"]["tenant_key"],"owner":cfg["steward"]["open_id"],
        "origin":dest["tenant_origin"],"title":title,"vendor":vendor,"channel":str(channel_path)}


def verify_outbound(assignment, cfg, employee, alias, source, dest, title, identity):
    """Match the actual, captured OAuth identity and destination used for this request."""
    expected = (assignment or {}).get("_retry_resource")
    if expected is not None:
        current = outbound_scope(cfg, employee, alias, source, dest, title, identity, assignment.get("runtime_vendor"))
        recovery.require(expected == current, "原工单的飞书身份、来源或知识库已改变，已停止本次操作。")


def observed_target(events):
    calls=set()
    observed=[]
    for kind,data in events:
        if kind=="tool.started" and data.get("name")=="mcp__team__feishu_list_sources":
            calls.add(data.get("call_id"))
        elif kind=="tool.completed" and data.get("call_id") in calls:
            calls.remove(data["call_id"])
            if not data.get("ok") or data.get("truncated"):
                continue
            try:
                body=json.loads(data["output"])
            except (ValueError,TypeError,KeyError):
                continue
            if isinstance(body,dict) and isinstance(body.get("knowledge_target"),dict):
                observed.append(body["knowledge_target"])
    recovery.require(observed and all(item==observed[-1] for item in observed),
                     "上轮工具记录没有一致的知识库目标可供核对。")
    return observed[-1]


def prior_resource(journal,scope,employee,operation,count,observed,settings_file,original_time):
    """Anchor the current config to the grant actually read and the registry row inspected on the old turn."""
    reads=journal.execute("select app,chat,cutoff,frontier from employee_history_reads where employee=? and job=?",
                          (employee,operation)).fetchall()
    recovery.require(len(reads)==1 and tuple(reads[0][:2])==(scope["app"],scope["chat"])
        and type(count) is int and count>0 and reads[0][3]==count,
        "上轮的应用、会话、调用或完整读取回执与当前绑定不符。")
    known=journal.execute("select document from knowledge_documents where binding=?",(scope["binding"],)).fetchone()
    recovery.require(known and isinstance(known[0],str) and known[0] and observed.get("title")==scope["title"],
                     "旧知识库绑定或原岗位文档标题已改变。")
    if observed.get("url"):
        recovery.require(observed["url"]==scope["origin"]+"/docx/"+known[0] and observed.get("remote_state")=="present")
    else:
        recovery.require(observed.get("remote_state")=="missing",
                         "上轮没有核对过绑定知识库是否存在。")
        alternatives=journal.execute("select 1 from knowledge_documents where binding<>? and document is not null limit 1",
                                     (scope["binding"],)).fetchone()
        if alternatives:
            recovery.require(settings_file.is_file() and settings_file.stat().st_mtime<=original_time,
                             "存在多个旧知识库，岗位目标在上轮后改过，无法唯一核对。")
    return recovery.fingerprint([scope["binding"],known[0],operation,reads[0][2],count]),known[0]


def rearm_once(journal,app,event,parent,attempt,prior_outgoing):
    journal.execute("""create table if not exists unpublished_retry_notices(
        app text,event text,job integer,attempt text,prior_outgoing text,primary key(app,event,job))""")
    claim=journal.execute("insert or ignore into unpublished_retry_notices values(?,?,?,?,?)",
                          (app,event,parent,attempt,prior_outgoing))
    if not claim.rowcount:
        saved=journal.execute("select attempt,prior_outgoing from unpublished_retry_notices where app=? and event=? and job=?",
                              (app,event,parent)).fetchone()
        recovery.require(saved and tuple(saved)==(attempt,prior_outgoing))
        return
    result=journal.execute("""update direct_team_reports set finished=0 where app=? and event=? and job=? and finished=1
        and exists(select 1 from direct_finals where app=? and event=? and outgoing=?)""",
        (app,event,parent,app,event,prior_outgoing))
    recovery.require(result.rowcount==1,"原回信已变化，未重新登记投递。")


async def tool_proof(db,cfg,employee,job,pin,vendor):
    """Prove the current turn can still use the resources of the authenticated original request."""
    saved=await db.get(FeatureMessage,(job.owner_id,job.origin_ref))
    if (not saved or saved.agent_id!=employee.id or saved.result.get("withdrawn")
            or (saved.result.get("job_id"),saved.result.get("task_id"))!=(job.id,job.task_id)):
        return None
    scope=resource_scope(cfg,employee,pin)
    if scope["vendor"]!=vendor or saved.result.get("resource")!=recovery.fingerprint(scope) or not saved.result.get("document"):
        return None
    with recovery.channel(cfg) as journal:
        row,_,_=recovery.source(journal,cfg,job.requester_turn_id)
        current=journal.execute("""select 1 from unpublished_retry_notices n join direct_finals f using(app,event)
            join direct_team_reports r using(app,event,job) where n.app=? and n.event=? and n.job=?
            and n.attempt=? and n.prior_outgoing=? and f.outgoing=n.prior_outgoing and r.finished=0""",
            (row["app"],row["event"],job.parent_id,job.origin_ref,saved.result["prior_outgoing"])).fetchone()
        if not current:
            return None
    if await later_owner_work(db,job):
        return None
    return {**pin,"_retry_resource":scope,"_retry_document":saved.result["document"]}


async def later_owner_work(db,job):
    newer_message=await db.scalar(select(Event.id).where(Event.thread_id==job.requester_thread_id,
        Event.turn_id>job.requester_turn_id,Event.type=="turn.started",Event.data["internal"].as_boolean().is_not(True)).limit(1))
    newer_job=await db.scalar(select(AgentJob.id).where(AgentJob.owner_id==job.owner_id,
        AgentJob.task_id==job.task_id,AgentJob.id>job.id).limit(1))
    return bool(newer_message or newer_job)


async def retry(sessions,cfg,owner_turn,task,previous_turn,*,apply=False):
    try:
        return await _retry(sessions,cfg,owner_turn,task,previous_turn,apply=apply)
    except recovery.RecoveryDenied:
        if apply:
            await _abandon_changed_request(sessions,cfg,owner_turn,task,previous_turn)
        raise


async def _abandon_changed_request(sessions,cfg,owner_turn,task,previous_turn):
    """A new owner instruction or changed resource revokes a staged retry, leaving the old receipt intact."""
    if settings.STEWARD_STRICT_OWNER != cfg.get("owner_id"):
        return
    async with sessions() as db:
        await begin_write(db)
        owner=cfg["owner_id"]
        saved=await db.get(FeatureMessage,(owner,f"cloud-delivery-{owner_turn}-{previous_turn}"))
        if not saved or saved.result.get("task_id")!=task:
            return
        child=await db.get(AgentJob,saved.result["job_id"],with_for_update=True)
        parent=await db.get(AgentJob,child.parent_id,with_for_update=True) if child and child.parent_id else None
        if not (parent and parent.status==STAGED and parent.owner_id==child.owner_id==owner and
                parent.requester_turn_id==owner_turn and parent.assignee_agent_id==cfg["steward"]["agent_id"]
                and child.status=="failed" and child.assignee_turn_id==previous_turn):
            return
        with recovery.channel(cfg,writable=True) as journal,journal:
            if not recovery.original_reply(journal,cfg,parent):
                return
            journal.execute("update direct_team_reports set finished=1 where job=? and app=?",
                (parent.id,cfg["steward"]["app_id"]))
        parent.status="failed"
        saved.result={**saved.result,"withdrawn":True}
        db.add(AgentJobEvent(job_id=child.id,kind="delivery_retry_withdrawn",detail={"previous_turn":previous_turn}))
        await db.commit()


async def _retry(sessions,cfg,owner_turn,task,previous_turn,*,apply):
    """Stage in the work DB, rearm the original delivery, then release to the normal employee runner."""
    owner = cfg["owner_id"]
    recovery.require(owner == settings.STEWARD_STRICT_OWNER)
    resume_key = f"cloud-delivery-{owner_turn}-{previous_turn}"
    async with sessions() as db:
        await begin_write(db)
        team = await service.team_for(db,owner,lock=True)
        with recovery.channel(cfg) as journal:
            row,digest,_ = recovery.source(journal,cfg,owner_turn)
            registration = journal.execute("select job,finished from direct_team_reports where app=? and event=?",
                (row["app"],row["event"])).fetchone()
            recovery.require(registration)
        parent = await db.get(AgentJob,registration["job"],with_for_update=True)
        receipt = await db.get(DirectTurnReceipt,(owner,recovery.event_key(row)))
        recovery.require(parent and team and team.lead_agent_id==cfg["steward"]["agent_id"]==parent.assignee_agent_id
            and parent.owner_id==owner and parent.kind=="root" and parent.origin_kind=="person"
            and parent.requester_turn_id==owner_turn and parent.requester_thread_id==row["thread"]
            and receipt and (receipt.turn_id,receipt.thread_id,receipt.input_sha256)==(owner_turn,row["thread"],digest))
        children = list(await db.scalars(select(AgentJob).where(AgentJob.parent_id==parent.id).with_for_update()))
        recovery.require(len(children)==1,"这次派工有多名经办人，请由管家分别核对。")
        child=children[0]
        recovery.require(child.task_id==task and child.owner_id==owner and child.root_id==parent.id
            and child.kind=="job" and child.mode=="work" and child.reply=="resume" and child.origin_kind=="person"
            and child.requester_agent_id==team.lead_agent_id and child.requester_thread_id==row["thread"]
            and child.requester_turn_id==owner_turn and child.approval_mode!="plan" and await feature_tasks.assignment_current(db,child))
        saved=await db.get(FeatureMessage,(owner,resume_key))
        if saved and parent.status!=STAGED:
            recovery.require(not saved.result.get("withdrawn") and saved.agent_id==child.assignee_agent_id
                and saved.result.get("job_id")==child.id)
            return {"employee_job":child.id,"state":child.status,"already_registered":True}
        staged=parent.status==STAGED and saved is not None
        prior_attempt=await db.scalar(select(AgentJobEvent.detail).where(
            AgentJobEvent.job_id==child.id,AgentJobEvent.kind==RETRY_EVENT).limit(1))
        recovery.require(not prior_attempt or (staged and prior_attempt.get("previous_turn")==previous_turn),
                         "原来信已经登记过一次续办，请先核对原工单的回执。")
        recovery.require((staged or (parent.status=="failed" and parent.outcome=="missing_delivery" and registration["finished"]))
            and child.status=="failed" and child.outcome in ("verification_blocked","missing_delivery")
            and child.assignee_turn_id==previous_turn, "工单已变更或仍有其他工作在办。")
        turn = await db.get(Turn,previous_turn)
        original = await db.get(Turn,owner_turn)
        employee = await service.member(db,owner,child.assignee_agent_id,team_id=team.id)
        card = await db.get(WorkItem,task)
        recovery.require(turn and turn.status=="succeeded" and turn.thread_id==child.assignee_thread_id and original and original.finished_at
            and timedelta(0)<=service.now()-original.finished_at<=timedelta(days=1)
            and employee.approval_mode!="plan" and employee.manager_agent_id in (None,team.lead_agent_id)
            and card and not card.archived and not card.completed_at and card.thread_id==turn.thread_id)
        newer=await db.scalar(select(Turn.id).where(Turn.thread_id==turn.thread_id,Turn.id>previous_turn).limit(1))
        pending=await db.scalar(select(Approval.id).where(Approval.turn_id==previous_turn,Approval.status=="pending").limit(1))
        recovery.require(not (newer or pending or await later_owner_work(db,child)), "已有新的操作、消息或审批，请先核对。")
        pin = await feishu_assignment.for_job(db,child.id)
        recovery.require(pin and pin.get("employee")==employee.id and not pin.get("read_only")
            and feishu_assignment.required_tool(pin) in (None,"feishu_publish_knowledge"))
        if recovery.is_recovery(parent):
            proof=await recovery.proof_for(db,parent)
            recovery.require(proof.get("require_document") and (await recovery.checked_child(db,parent,proof))[0].id==child.id)
        else:
            recovery.require(feishu_assignment.required_tool(pin)=="feishu_publish_knowledge")
        trusted=await db.scalar(select(AgentJobEvent.detail).where(AgentJobEvent.job_id==child.id,AgentJobEvent.kind=="owner_request"))
        recovery.require(trusted and (trusted.get("thread_id"),trusted.get("turn_id"),trusted.get("event_key"))
            ==(row["thread"],owner_turn,receipt.event_key))
        wrote=await db.scalar(select(AgentJobEvent.id).where(AgentJobEvent.job_id==child.id,
            AgentJobEvent.detail["tool"].as_string().in_(WRITE_TOOLS)).limit(1))
        from app.team.feishu_tools_bridge import invocation_key
        operation=await invocation_key(db,child,pin)
        history=await db.scalar(select(AgentJobEvent).where(AgentJobEvent.job_id==child.id,AgentJobEvent.kind=="feishu_delivered",
            AgentJobEvent.detail["tool"].as_string()=="feishu_read_history",AgentJobEvent.detail["employee"].as_integer()==employee.id,
            AgentJobEvent.detail["epoch"].as_integer()==child.assignment_epoch,
            AgentJobEvent.detail["operation"].as_string()==operation).order_by(AgentJobEvent.id.desc()).limit(1))
        events=(await db.execute(select(Event.type,Event.data).where(Event.turn_id==previous_turn,
                Event.type.in_(("tool.started","tool.completed"))).order_by(Event.seq))).all()
        read_tools={"mcp__team__feishu_read_history","mcp__team__feishu_read_message"}
        read_ids={d.get("call_id") for kind,d in events if kind=="tool.started" and d.get("name") in read_tools and d.get("call_id")}
        recovery.require(history and not wrote and only_reads(events) and turn.finished_at
            and history.detail.get("turn_id",previous_turn)==previous_turn
            and int(turn.started_at.timestamp())<=int(history.at.timestamp())<=int(turn.finished_at.timestamp())
            and any(kind=="tool.completed" and d.get("call_id") in read_ids and d.get("ok") for kind,d in events),
            "已有写入、未知执行结果或缺少本轮完整读取回执，不能重新发布。")
        scope=resource_scope(cfg,employee,pin)
        resource=recovery.fingerprint(scope)
        observed=observed_target(events)
        count=(history.detail.get("result") or {}).get("messages_read")
        settings_file=Path(cfg["_access_config_path"]).parent/"employee-settings"/str(employee.id)/"knowledge-task.json"
        started=turn.started_at.timestamp()
        with recovery.channel(cfg) as journal:
            journal.execute("begin")
            prior,document=prior_resource(journal,scope,employee.id,operation,count,observed,settings_file,started)
            outgoing=journal.execute("""select o.id from direct_finals f join outbox o on o.id=f.outgoing
                join assignment_outbox a on o.id=a.outgoing where f.app=? and f.event=? and a.job=?
                and o.role='steward' and o.state='sent' and length(o.remote)>0 and json_extract(o.body,'$.reply_to')=?""",
                (row["app"],row["event"],child.id,row["message"])).fetchone()
            recovery.require(outgoing)
            prior_outgoing=outgoing[0]
        snapshot={"resource":resource,"prior_resource":prior,"document":document,"prior_outgoing":prior_outgoing}
        if not apply:
            return {"eligible":True,"employee_job":child.id,"previous_turn":previous_turn}
        if staged:
            recovery.require(all(saved.result.get(key)==value for key,value in snapshot.items()))
        else:
            db.add(FeatureMessage(owner_id=owner,event_key=resume_key,agent_id=employee.id,project=child.project,
                text=NOTE,result={"job_id":child.id,"task_id":task,**snapshot}))
            db.add(AgentJobEvent(job_id=child.id,kind=RETRY_EVENT,detail={"previous_turn":previous_turn,
                "previous_state":child.status,"previous_outcome":child.outcome,"resource":resource}))
            parent.status=STAGED
            await db.commit()
        parent_id,child_id=parent.id,child.id
    with recovery.channel(cfg,writable=True) as journal,journal:
        rearm_once(journal,row["app"],row["event"],parent_id,resume_key,prior_outgoing)
    async with sessions() as db:
        await begin_write(db)
        parent=await db.get(AgentJob,parent_id,with_for_update=True)
        child=await db.get(AgentJob,child_id,with_for_update=True)
        if parent.status==STAGED:
            # Recheck between databases before the employee runner can claim the job.
            with recovery.channel(cfg) as journal:
                row,_,_=recovery.source(journal,cfg,owner_turn)
                report=journal.execute("select job,finished from direct_team_reports where app=? and event=?",
                    (row["app"],row["event"])).fetchone()
                recovery.require(report and report["job"]==parent_id and not report["finished"])
            employee=await service.member(db,owner,child.assignee_agent_id,team_id=team.id)
            current_pin=await feishu_assignment.for_job(db,child.id)
            saved=await db.get(FeatureMessage,(owner,resume_key))
            recovery.require(current_pin and saved and not saved.result.get("withdrawn"))
            current_scope=resource_scope(cfg,employee,current_pin)
            with recovery.channel(cfg) as journal:
                journal.execute("begin")
                current_prior,_=prior_resource(journal,current_scope,employee.id,operation,count,observed,settings_file,started)
            recovery.require(saved.result["resource"]==recovery.fingerprint(current_scope) and
                             saved.result["prior_resource"]==current_prior)
            recovery.require(child.status=="failed" and child.assignee_turn_id==previous_turn
                and not await later_owner_work(db,child) and await feature_tasks.assignment_current(db,child,lock=True))
            old=await db.scalar(select(AgentJobInbox).where(AgentJobInbox.owner_id==owner,
                AgentJobInbox.holder_id==parent.id,AgentJobInbox.job_id==child.id,AgentJobInbox.kind=="settled"))
            # A consumed notification stays in the audit history. This attempt will generate its own settlement.
            if old:
                old.kind,old.delivered_at=f"previous_settlement:{previous_turn}",old.delivered_at or service.now()
            parent.status,parent.outcome,parent.outcome_detail,parent.finished_at,parent.current_turn_id="waiting",None,None,None,None
            child.status,child.origin_ref,child.answer="queued",resume_key,NOTE
            child.assignee_turn_id,child.current_turn_id,child.finished_at=None,None,None
            child.outcome,child.outcome_detail,child.result,child.last_activity_at=None,None,None,service.now()
            await db.commit()
        return {"employee_job":child_id,"state":child.status}
