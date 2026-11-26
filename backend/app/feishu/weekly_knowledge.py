"""The owner's weekly grant for one existing employee knowledge document."""
import hashlib
import json
import logging
import os
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from app.db.models import Agent, AgentJob, AgentJobEvent, FeatureMessage, FeatureScope, WorkItem
from app.db.models.knowledge_weekly import KnowledgeWeeklyGrant, KnowledgeWeeklySlot
from app.db.transactions import begin_write
from app.feishu import knowledge_document, owner_oauth, workspace_access
from app.team import service
from app.team.document_delivery import PUBLISH, evidence


WEEK = timedelta(days=7)
PREFIX = "knowledge-week-"
TOOLS = ("feishu_list_sources", "feishu_read_history", "feishu_read_message", "feishu_read_document", "feishu_publish_knowledge")


def enabled():
    return os.getenv("SUPARAGENT_WEEKLY_KNOWLEDGE_ENABLED") == "1"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def latest_due(first, now):
    return first + ((now - first) // WEEK) * WEEK if now >= first else None


def _journal(cfg):
    path = Path(cfg["_access_config_path"]).parent / cfg["state_dir"] / "channel.sqlite3"
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)


def resource_scope(cfg, employee, vendor):
    from app.feishu.documents import TOKEN
    path = Path(cfg["_access_config_path"]).parent / "employee-settings" / str(employee) / "knowledge-task.json"
    task = json.loads(path.read_text(encoding="utf-8-sig"))
    frequency = task.get("incremental_frequency")
    if not isinstance(frequency, str) or not re.fullmatch(r"weekly|每(?:周|星期)(?:(?:更新|整理)?(?:一|1)次)?|一周一次", frequency.strip(), re.I):
        raise ValueError("员工档案没有明确设置为每周一次。")
    alias = task.get("contact")
    if not isinstance(alias, str) or not alias or task.get("initial_range") != "all_available":
        raise ValueError("请核对该员工的联系人和首次整理范围。")
    settings = workspace_access.settings_for(cfg)
    identity = owner_oauth.employee_config(cfg, employee)
    role = identity["steward"]
    source = settings["history_sources"].get(alias)
    if (not identity.get("_employee_oauth") or not source or source.get("agent_id") != employee or
            source.get("oauth_app") != role["app_id"] or not source.get("chat_id") or not source.get("peer_open_id") or
            not workspace_access.permits_model(settings, (employee, None, None, None), {"runtime_vendor":vendor})):
        raise ValueError("联系人、员工飞书应用或模型读取授权不匹配。")
    actions = owner_oauth.status(identity)["actions"]
    if not all(actions.get(kind) for kind in ("history", "doc", "folder")):
        raise ValueError("该员工缺少聊天记录、文档或文件夹核对权限。")
    dest, title = knowledge_document.target(cfg, employee, alias)
    folder = settings.get("document_target")
    if folder and dest != folder:
        raise ValueError("员工知识库的文件夹与老板保存的目的地不一致。")
    binding = knowledge_document.binding_key(cfg["steward"]["app_id"], employee, alias, source, dest, title)
    with closing(_journal(cfg)) as db:
        row = db.execute("select document from knowledge_documents where binding=?", (binding,)).fetchone()
    if not row or not row[0] or not TOKEN.fullmatch(row[0]):
        raise ValueError("首次整理尚无已绑定的知识库文档。")
    return {"owner":cfg["owner_id"], "lead":cfg["steward"]["agent_id"], "owner_app":cfg["steward"]["app_id"],
            "owner_open_id":cfg["steward"]["open_id"], "tenant":cfg["steward"]["tenant_key"], "employee":employee,
            "employee_app":role["app_id"], "employee_open_id":role["open_id"], "contact":alias,
            "chat":source["chat_id"], "peer":source["peer_open_id"], "destination":dest, "title":title,
            "document":row[0], "binding":binding, "vendor":vendor, "frequency":"weekly"}


async def _live(db, cfg, task_id, employee, epoch):
    from app.services.engine_registry import resolve_model_profile
    from app.services.role_workspace import load
    owner = cfg["owner_id"]
    team = await service.team_for(db, owner)
    item = await db.get(WorkItem, task_id)
    scope = await db.get(FeatureScope, task_id)
    agent = await db.get(Agent, employee)
    if (not item or item.owner_id != owner or item.agent_id != employee or item.archived or item.completed_at or
            not scope or scope.assignment_epoch != epoch or not agent or agent.owner_id != owner or agent.archived_at or
            not agent.role_path or not team or agent.team_id != team.id or team.lead_agent_id != cfg["steward"]["agent_id"] or
            (agent.manager_agent_id or team.lead_agent_id) != team.lead_agent_id or agent.approval_mode == "plan"):
        raise ValueError("员工、主管、工单或岗位权限已变更。")
    lead = await db.get(Agent, team.lead_agent_id)
    if not lead or not lead.delegation_enabled or lead.archived_at:
        raise ValueError("管家当前不能派工。")
    model = load(Path(agent.role_path), agent.model).model
    vendor = resolve_model_profile(agent.provider, agent.model_vendor, model, agent.effort if model == agent.model else None).model_vendor
    if vendor not in ("deepseek", "openai"):
        raise ValueError("无法核实该员工当前处理私聊的模型供应商。")
    return item, scope, resource_scope(cfg, employee, vendor)


async def enroll(db, cfg, employee, source_job_id, *, now=None):
    """Called only by the owner management route, including a one-time import of an existing grant."""
    from app.team.feishu_assignment import for_job, required_tool
    await begin_write(db)
    source = await db.get(AgentJob, source_job_id, with_for_update=True)
    if (not source or source.owner_id != cfg["owner_id"] or source.assignee_agent_id != employee or
            source.origin_kind != "person" or source.kind != "job" or source.mode != "work" or not source.task_id):
        raise ValueError("请选择老板先前交给这名员工的知识库派工。")
    _, _, scope = await _live(db, cfg, source.task_id, employee, source.assignment_epoch)
    pin = await for_job(db, source.id)
    if (not pin or pin.get("employee") != employee or pin.get("contact") != scope["contact"] or
            not (required_tool(pin) == "feishu_publish_knowledge" or
                 (pin["action"] == "employee" and required_tool(pin) is None)) or source.requester_agent_id != scope["lead"]):
        raise ValueError("原派工未绑定同一名员工、联系人和知识库。")
    link = scope["destination"]["tenant_origin"] + "/docx/" + scope["document"]
    first = await evidence(db, source, PUBLISH, pin=pin, expected_url=link)
    if not first.result:
        raise ValueError("尚无该员工对同一文档的首次发布回执。")
    grant = await db.get(KnowledgeWeeklyGrant, (cfg["owner_id"], employee), with_for_update=True)
    if grant and grant.enabled:
        if (grant.scope, grant.task_id, grant.assignment_epoch) != (scope, source.task_id, source.assignment_epoch):
            raise ValueError("原定时授权的范围已变化，请先停用原计划。")
        return grant
    if not grant:
        grant = KnowledgeWeeklyGrant(owner_id=cfg["owner_id"], employee_id=employee)
        db.add(grant)
    grant.source_job_id, grant.task_id, grant.assignment_epoch = source.id, source.task_id, source.assignment_epoch
    grant.scope, grant.enabled, grant.blocked_reason = scope, True, None
    grant.generation = uuid4().hex
    grant.next_due = (now or datetime.now(UTC)) + WEEK
    await db.commit()
    return grant


async def proof_for_job(db, cfg, job):
    if (job.origin_kind != "routine" or not (job.origin_ref or "").startswith(PREFIX)):
        return None
    slot = await db.get(KnowledgeWeeklySlot, job.origin_ref)
    if not enabled() or not slot or slot.job_id != job.id or slot.owner_id != cfg.get("owner_id") or slot.employee_id != job.assignee_agent_id:
        return None
    grant = await db.get(KnowledgeWeeklyGrant, (slot.owner_id, slot.employee_id))
    if (not grant or not grant.enabled or (job.task_id, job.assignment_epoch) != (grant.task_id, grant.assignment_epoch)
            or job.requester_agent_id != grant.scope.get("lead") or slot.scope_digest != digest([grant.generation, grant.scope])):
        return None
    try:
        _, _, scope = await _live(db, cfg, grant.task_id, slot.employee_id, grant.assignment_epoch)
    except (ValueError, OSError, KeyError, sqlite3.Error):
        return None
    if scope != grant.scope:
        return None
    return {"employee":slot.employee_id, "contact":scope["contact"], "action":"update", "expected_tool":"feishu_publish_knowledge",
            "event_key":slot.event_key, "weekly":scope, "cutoff":slot.cutoff, "since":slot.since}


async def claim(db, cfg, employee, now):
    from app.team.jobs import event
    await begin_write(db)
    owner = cfg["owner_id"]
    grant = await db.get(KnowledgeWeeklyGrant, (owner, employee), with_for_update=True)
    due = latest_due(grant.next_due, now) if grant and grant.enabled else None
    if due is None:
        return None
    try:
        _, feature, scope = await _live(db, cfg, grant.task_id, employee, grant.assignment_epoch)
        if grant.scope != scope:
            raise ValueError("定时授权的资源、员工身份或模型供应商已变更。")
    except (ValueError, KeyError, OSError, sqlite3.Error):
        grant.enabled, grant.blocked_reason = False, "员工、联系人、文档或模型授权已变更，周计划已停用。"
        await db.commit()
        return None
    recent = (await db.execute(select(KnowledgeWeeklySlot, AgentJob).join(AgentJob, AgentJob.id == KnowledgeWeeklySlot.job_id)
        .where(KnowledgeWeeklySlot.owner_id == owner, KnowledgeWeeklySlot.employee_id == employee,
               KnowledgeWeeklySlot.scope_digest == digest([grant.generation, scope])).order_by(KnowledgeWeeklySlot.cutoff.desc()))).all()
    if any(j.status not in ("completed", "failed", "cancelled") for _, j in recent):
        return None
    # A previous failed slot doesn't advance the watermark. Re-read its interval next week.
    since = None
    expected_url = scope["destination"]["tenant_origin"] + "/docx/" + scope["document"]
    for slot, previous in recent:
        if previous.status != "completed":
            continue
        receipt = await evidence(db, previous, PUBLISH, expected_url=expected_url)
        if receipt.result:
            since = slot.cutoff
            break
    key = PREFIX + digest([owner, employee, grant.task_id, grant.assignment_epoch, grant.generation, due.isoformat()])
    earlier = await db.get(KnowledgeWeeklySlot, key)
    if earlier:
        return earlier.job_id
    source = await db.get(AgentJob, grant.source_job_id)
    if not source or source.owner_id != owner or source.task_id != grant.task_id:
        return None
    brief = "执行已授权的每周知识库更新。先核对原文档，再逐页读取新增聊天并提交分类、摘要和原消息编号。没有新增时核对原文档并汇报。"
    job = AgentJob(owner_id=owner, requester_agent_id=scope["lead"], requester_thread_id=source.requester_thread_id,
        requester_turn_id=source.requester_turn_id, assignee_agent_id=employee, project=feature.project, task_id=grant.task_id,
        assignment_epoch=grant.assignment_epoch, depth=1, origin_kind="routine", origin_ref=key, kind="job", mode="work",
        brief=brief, brief_hash=digest(brief), done_when="飞书读回同一知识库的新增综述和来源。", approval_mode="plan",
        reply="resume", status="queued")
    db.add(job)
    await db.flush()
    db.add(FeatureMessage(owner_id=owner, event_key=key, agent_id=employee, project=feature.project, text=brief,
        result={"task_id":grant.task_id, "job_id":job.id, "assignment_epoch":grant.assignment_epoch}))
    db.add(KnowledgeWeeklySlot(event_key=key, owner_id=owner, employee_id=employee, job_id=job.id,
                               scope_digest=digest([grant.generation, scope]), cutoff=int(due.timestamp()), since=since))
    event(db, job, "weekly_knowledge", {"source_job":source.id, "scope_digest":digest(scope), "task_id":grant.task_id})
    event(db, job, "feishu_assignment", {"employee":employee, "contact":scope["contact"], "action":"update",
                                         "expected_tool":"feishu_publish_knowledge", "weekly_event":key})
    grant.next_due, grant.blocked_reason = due + WEEK, None
    await db.commit()
    return job.id


def source_ids(plan, chat, *, records=None):
    from . import knowledge_citations
    ids = set()
    for block in plan:
        for field in ("text", "heading1", "heading2"):
            for index, element in enumerate(block.get(field, {}).get("elements", [])):
                run = element.get("text_run", {})
                if index or run.get("text_element_style", {}).get("link"):
                    found, _ = knowledge_citations.identify(run, chat, records=records)
                    ids.update(found)
    return ids


def merge_blocks(prior, added, chat):
    from . import knowledge_citations
    from .knowledge_revision import text_block
    seen, new = source_ids(prior, chat), source_ids(added, chat)
    if not seen or not new or seen & new:
        raise ValueError("本次新增消息与已核对的来源不符。")
    groups = {}
    for plan in (prior, added):
        current = None
        for block in plan[2:]:
            if block.get("block_type") == 4:
                title = block["heading2"]["elements"][0]["text_run"]["content"]
                current = groups.setdefault(title, [block])
            elif block.get("block_type") == 2 and current is not None:
                current.append(knowledge_citations.archive_old_links(block, chat) if plan is prior else block)
            else:
                raise ValueError("无法在保留原综述的前提下合并这一版。")
    return [prior[0], text_block("text", f"本版涵盖 {len(seen | new)} 条消息。"),
            *(block for group in groups.values() for block in group)]


def baseline(db, client, auth, proof):
    """Only a previously verified generated plan that still matches the live document is a baseline."""
    from . import document_access, knowledge_revision as revisions
    scope = proof["weekly"]
    doc = scope["document"]
    if not document_access.in_folder(client, auth, scope["destination"]["folder_token"], doc):
        raise ValueError("周计划绑定的文档已移出文件夹或当前无法访问。")
    metadata = knowledge_document.inspect_document(client, auth, doc)
    if not metadata or metadata.get("title") != scope["title"]:
        raise ValueError("周计划绑定的文档已删除、改名或无权读取。")
    remote = revisions.root_blocks(client, auth, doc)
    key = scope["binding"] + "/" + doc
    db.execute("""create table if not exists weekly_knowledge_baseline(event_key text primary key,
               binding text not null, plan text not null, merged text)""")
    stored = db.execute("select plan,merged from weekly_knowledge_baseline where event_key=? and binding=?",
                        (proof["event_key"], key)).fetchone()
    if stored:
        old = json.loads(stored[0])
        if revisions.matches(remote, old) and all(revisions.removable(b) for b in remote[:len(old)]):
            return old
        if stored[1]:
            merged = json.loads(stored[1])
            pending = db.execute("select plan from knowledge_revisions where binding=? and status in ('sending','planned','verify')",
                                 (key,)).fetchall()
            if revisions.matches(remote, merged) or any(json.loads(value) == merged for value, in pending):
                return old
        raise ValueError("本周读取原稿之后云端正文已变化，请核对原件。")
    if not db.execute("select 1 from sqlite_master where name='knowledge_revisions' and type='table'").fetchone():
        raise ValueError("请先完成首次汇编及云端读回，再进行每周增量。")
    saved = db.execute("""select plan from knowledge_revisions where binding=? and status='done' order by rowid desc""", (key,))
    for (value,) in saved:
        plan = json.loads(value)
        if (plan and revisions.matches(remote, plan) and all(revisions.removable(b) for b in remote[:len(plan)])
                and source_ids(plan, scope["chat"])):
            with db:
                db.execute("insert or ignore into weekly_knowledge_baseline(event_key,binding,plan) values(?,?,?)",
                           (proof["event_key"], key, value))
            return plan
    raise ValueError("云端原综述与已核对版本不符，请先检查人工改动或中断的写入。")


def merged_draft(db, proof, prior, additions):
    merged = merge_blocks(prior, additions, proof["weekly"]["chat"])
    with db:
        db.execute("update weekly_knowledge_baseline set merged=? where event_key=? and merged is null",
                   (json.dumps(merged, ensure_ascii=False), proof["event_key"]))
        saved = db.execute("select merged from weekly_knowledge_baseline where event_key=?", (proof["event_key"],)).fetchone()
    if not saved or json.loads(saved[0]) != merged:
        raise ValueError("本周已有一份待核对的稿件，请沿用原稿。")
    return merged


async def original_message(db, worker, job):
    """Find the owner's original DM, including work continued by an internal supervisor turn."""
    from app.db.models import DirectTurnReceipt
    from .submissions import event_key

    cfg = worker.cfg
    if not job or job.owner_id != cfg["owner_id"]:
        return None
    thread, turn = job.requester_thread_id, job.requester_turn_id
    origin = await db.scalar(select(AgentJobEvent.detail).where(
        AgentJobEvent.job_id == job.id, AgentJobEvent.kind == "owner_request").order_by(AgentJobEvent.id).limit(1))
    key = origin.get("event_key") if origin else None
    if key:
        receipt = await db.get(DirectTurnReceipt, (job.owner_id, key))
        if not receipt or (receipt.thread_id, receipt.turn_id) != (thread, origin.get("turn_id")) or origin.get("thread_id") != thread:
            return None
        turn = receipt.turn_id
    identity = cfg["steward"]
    rows = worker.db.execute("""select i.app,i.event,i.message from direct_turns d
        join inbox i using(app,event) join owner_dm_sources s using(app,event,message)
        where d.thread=? and d.turn=? and i.app=? and s.owner=? and s.tenant=?""",
        (thread, turn, identity["app_id"], identity["open_id"], identity["tenant_key"])).fetchall()
    if len(rows) == 1 and (not key or event_key(rows[0]) == key):
        return rows[0]["message"]
    return None


async def tick(worker):
    owner = worker.cfg.get("owner_id")
    imports = []
    for entry in os.getenv("SUPARAGENT_WEEKLY_KNOWLEDGE_IMPORT", "").split(","):
        binding = entry.strip().split("/")
        if len(binding) == 3 and binding[0] == owner and all(re.fullmatch(r"[1-9][0-9]{0,17}", n) for n in binding[1:]):
            imports.append((int(binding[1]), int(binding[2])))
    if not owner or not (enabled() or imports):
        return
    from app.db import session
    from .routing import bind_outgoing, enqueue
    now = datetime.now(UTC)
    last = getattr(worker, "_weekly_checked", None)
    if last and now - last < timedelta(seconds=60):
        return
    worker._weekly_checked = now
    for employee, source_job_id in imports:
        async with session.AsyncSessionLocal() as db:
            if await db.get(KnowledgeWeeklyGrant, (owner, employee)):
                continue
            try:
                await enroll(db, worker.cfg, employee, source_job_id, now=now)
            except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
                logging.getLogger(__name__).info("Weekly import did not match its saved grant (employee %s, %s)",
                                                 employee, type(exc).__name__)
    if not enabled():
        return
    with worker.db:
        worker.db.execute("create table if not exists knowledge_weekly_notice(event_key text primary key, outgoing text not null)")
    async with session.AsyncSessionLocal() as db:
        employees = list(await db.scalars(select(KnowledgeWeeklyGrant.employee_id).where(
            KnowledgeWeeklyGrant.owner_id == owner, KnowledgeWeeklyGrant.enabled.is_(True), KnowledgeWeeklyGrant.next_due <= now)))
    for employee in employees:
        async with session.AsyncSessionLocal() as db:
            await claim(db, worker.cfg, employee, now)
    async with session.AsyncSessionLocal() as db:
        rows = (await db.execute(select(KnowledgeWeeklySlot, AgentJob, KnowledgeWeeklyGrant)
            .join_from(KnowledgeWeeklySlot, AgentJob, AgentJob.id == KnowledgeWeeklySlot.job_id)
            .join(KnowledgeWeeklyGrant, (KnowledgeWeeklyGrant.owner_id == KnowledgeWeeklySlot.owner_id) &
                  (KnowledgeWeeklyGrant.employee_id == KnowledgeWeeklySlot.employee_id))
            .where(KnowledgeWeeklySlot.owner_id == owner, AgentJob.status.in_(("completed", "failed", "cancelled")))
            .order_by(KnowledgeWeeklySlot.cutoff.desc()))).all()
        for slot, job, grant in rows:
            if worker.db.execute("select 1 from knowledge_weekly_notice where event_key=?", (slot.event_key,)).fetchone():
                continue
            url=grant.scope["destination"]["tenant_origin"]+"/docx/"+grant.scope["document"]
            receipt=await evidence(db,job,PUBLISH,expected_url=url)
            verified = (grant.enabled and slot.scope_digest == digest([grant.generation, grant.scope]) and
                        job.status == "completed" and job.assignee_agent_id==slot.employee_id and receipt.result)
            body = {"text":(f"老板，我核对了工单 #{job.task_id} 的更新回执，本周的知识库在这里。\n{url}" if verified else
                            f"老板，工单 #{job.task_id} 本周的知识库更新，我还没核实。{(job.outcome_detail or '')[:130]}")}
            original = await db.get(AgentJob, grant.source_job_id)
            target = await original_message(db, worker, original)
            if target:
                body["reply_to"] = target
            with worker.db:
                marker = worker.db.execute("insert or ignore into knowledge_weekly_notice values(?, '')", (slot.event_key,))
                if marker.rowcount:
                    outgoing = enqueue(worker.db, None, "steward", body)
                    bind_outgoing(worker.db, outgoing, [job.id])
                    worker.db.execute("update knowledge_weekly_notice set outgoing=? where event_key=?", (outgoing, slot.event_key))
