"""Resolve owner messages against durable cards before an employee job is committed."""
from dataclasses import dataclass
import re

from sqlalchemy import and_, case, or_, select

from app.db.models import (Agent, AgentJob, AgentJobEvent, DirectTurnReceipt, Event, FeatureMessage,
                           FeatureScope, FeatureSession, FeishuLeadChat, Person, Thread, Turn, WorkItem)
from app.team import service


@dataclass(frozen=True)
class AssignmentContext:
    turn_id: int
    thread_id: int
    event_key: str
    original_text: str
    attachments: tuple[dict, ...]
    project: str | None
    reference_task_ids: tuple[int, ...]
    named_agent_ids: tuple[int, ...]
    reference_epochs: dict[str, int]
    reply_to_turn_id: int | None
    reference_event_key: str | None
    quoted_owner: dict | None


def status_only(text):
    token = re.sub(r"(?:任务|工单|task)?\s*#\s*\d+", "", text or "", flags=re.I).strip()
    return bool(re.fullmatch(
        r"(?:请|帮我|麻烦)?(?:查一下|查查|看看|问一下)?(?:这项|这个|当前|最新|本次)?(?:任务|工单)?的?"
        r"(?:进展|进度|状态|怎么样了|到哪一步了|什么进度)(?:如何|怎么样|是什么)?[？?。！!\s]*"
        r"|(?:status|progress)(?:\s+update)?[?.!\s]*",token,re.I))


def steward_task_scope(owner, project, thread_id):
    """A channel has a default project and can also continue work it actually assigned."""
    project_scope = FeatureScope.project == project
    if thread_id is None:
        return project_scope
    channel = select(FeishuLeadChat.channel).where(FeishuLeadChat.thread_id == thread_id).correlate(None).scalar_subquery()
    assigned = (select(AgentJob.task_id).join(Thread, Thread.id == AgentJob.requester_thread_id)
                .join(FeishuLeadChat, FeishuLeadChat.thread_id == Thread.id)
                .where(AgentJob.owner_id == owner, Thread.owner_id == owner, Thread.kind == 'chat',
                       Thread.agent_id == AgentJob.requester_agent_id, FeishuLeadChat.channel == channel,
                       AgentJob.origin_kind == 'person', AgentJob.kind == 'job', AgentJob.mode == 'work'))
    return or_(project_scope, WorkItem.id.in_(assigned))


async def status_text(db, owner, context):
    from app.db.models import WorkColumn
    query = select(WorkItem.id, WorkItem.title, WorkColumn.key).join(FeatureScope,FeatureScope.task_id==WorkItem.id
        ).join(WorkColumn,WorkColumn.id==WorkItem.column_id).where(WorkItem.owner_id==owner)
    if context.reference_task_ids:
        query = query.where(WorkItem.id.in_(context.reference_task_ids))
    elif context.project:
        query = query.where(steward_task_scope(owner, context.project, context.thread_id), WorkItem.archived.is_(False))
    else:
        query = query.where(WorkItem.id.in_(select(AgentJob.task_id).where(
            AgentJob.owner_id==owner, AgentJob.requester_thread_id==context.thread_id)))
    rows = (await db.execute(query.order_by(WorkItem.id.desc()).limit(12))).all()
    return "\n".join(f"#{row.id} {row.title} · {row.key}" for row in rows) or "这段对话还没有可查询的项目工单。"


async def _tasks_for_turn(db, owner, turn):
    # A quote can point to the request, or to the steward's subsequent report.
    roots = select(AgentJob.id).where(
        AgentJob.owner_id == owner, AgentJob.kind == "root", AgentJob.requester_thread_id == turn.thread_id,
        or_(AgentJob.requester_turn_id == turn.id, AgentJob.current_turn_id == turn.id,
            AgentJob.id.in_(select(AgentJobEvent.job_id).where(
                AgentJobEvent.kind == "resumed", AgentJobEvent.detail["turn_id"].as_integer() == turn.id))))
    return list(await db.scalars(select(AgentJob.task_id).where(
        AgentJob.owner_id == owner, AgentJob.kind == "job", AgentJob.mode == "work",
        AgentJob.requester_thread_id == turn.thread_id, AgentJob.task_id.is_not(None),
        or_(AgentJob.requester_turn_id == turn.id, AgentJob.parent_id.in_(roots))).distinct()))


async def _owner_chat(db, owner, thread_id):
    thread = await db.get(Thread, thread_id)
    team = await service.team_for(db, owner)
    if (not thread or thread.owner_id != owner or thread.kind != "chat" or thread.job_id or thread.room_id
            or not team or team.lead_agent_id != thread.agent_id):
        raise ValueError("引用不属于这位老板与管家的对话。")
    return thread, team


def _attachments(thread_id, started):
    return tuple({"thread_id":thread_id, "id":a["id"], "name":a.get("name", ""), "kind":a.get("kind", "")}
                 for a in (started or {}).get("attachments", []) if a.get("id"))


async def resolve_assignment_context(db, owner_id, turn_id):
    turn = await db.get(Turn, turn_id)
    if not turn or turn.origin not in (None, "person"):
        raise ValueError("需要老板发给管家的原始消息。")
    thread, team = await _owner_chat(db, owner_id, turn.thread_id)
    if turn.person_id is not None:
        person = await db.get(Person, turn.person_id)
        if not person or person.role != "owner" or person.status != "active":
            raise ValueError("只有老板能修改派工归属。")
    started = await db.scalar(select(Event.data).where(Event.turn_id == turn.id, Event.type == "turn.started"))
    if (started or {}).get("internal") or (started or {}).get("team_generated"):
        raise ValueError("团队回执不能作为老板发出的指令。")
    source = (started or {}).get("assignment_source") or {}
    text = source.get("owner_text", turn.prompt_text)
    binding = await db.get(FeishuLeadChat, thread.id)
    project = binding.project if binding and binding.project else None
    direct = await db.scalar(select(DirectTurnReceipt).where(
        DirectTurnReceipt.owner_id == owner_id, DirectTurnReceipt.turn_id == turn.id))
    ids = []
    quoted_owner = None
    cited = source.get("reply_to_turn_id")
    if ref_key := source.get("reference_event_key"):
        prior = await db.get(DirectTurnReceipt, (owner_id, ref_key))
        legacy = await db.get(FeatureMessage, (owner_id, ref_key))
        if prior:
            cited = prior.turn_id
        elif legacy and legacy.result.get("task_id"):
            # The common card check below also validates the task's channel or default project.
            ids.append(legacy.result["task_id"])
        else:
            raise ValueError("这条引用找不到原工单，请核对任务编号。")
    if cited:
        prior = await db.get(Turn, cited)
        if not prior or prior.id >= turn.id:
            raise ValueError("原消息已不可用。")
        if prior.thread_id != thread.id:
            await _owner_chat(db, owner_id, prior.thread_id)
            other = await db.get(FeishuLeadChat, prior.thread_id)
            if not binding or not other or (binding.channel, binding.project) != (other.channel, other.project):
                raise ValueError("这条引用来自另一段对话。")
        related = await _tasks_for_turn(db, owner_id, prior)
        ids.extend(related)
        if not related and prior.origin in (None, "person"):
            original = await db.scalar(select(Event.data).where(Event.turn_id==prior.id, Event.type=="turn.started"))
            person = await db.get(Person, prior.person_id) if prior.person_id is not None else None
            if (original is not None and not original.get("internal") and not original.get("team_generated")
                    and (prior.person_id is None or (person and person.role=="owner" and person.status=="active"))):
                quoted_owner = {"turn_id":prior.id, "thread_id":prior.thread_id,
                    "text":(original.get("assignment_source") or {}).get("owner_text", prior.prompt_text),
                    "attachments":list(_attachments(prior.thread_id, original))}
    if source.get("reference_task_id"):
        ids.append(source["reference_task_id"])
    # A URL fragment, issue number or quoted history is not an owner-supplied task number.
    if not ids:
        first = text.strip().splitlines()[0] if text.strip() else ""
        first = re.sub(r"https?://[^\s<>，。；、！]+", "", first, flags=re.I)
        ids = [int(v) for v in re.findall(r"(?:任务\s*#?\s*|工单\s*#?\s*|\btask\s*#\s*|(?<![a-z0-9_/.:?&=-])#)(\d+)(?![\da-z_])", first, re.I)]
    if ids:
        query = select(WorkItem.id).join(FeatureScope, FeatureScope.task_id == WorkItem.id).where(
            WorkItem.owner_id == owner_id, WorkItem.id.in_(ids))
        if project:
            query = query.where(steward_task_scope(owner_id, project, thread.id))
        if set(await db.scalars(query)) != set(ids):
            raise service.TeamError("assignment_reference_scope", "任务编号不属于当前老板或项目。")
    members = await db.execute(select(Agent.id, Agent.name).where(Agent.owner_id == owner_id,
        Agent.team_id == team.id, Agent.id != team.lead_agent_id, Agent.archived_at.is_(None)))
    first = text.strip().partition("\n")[0]
    named = []
    for agent_id, name in members:
        # Only a directly addressed employee may replace an existing assignee.
        # Merely mentioning a colleague or quoting their output does not name a recipient.
        who = re.escape(name)
        directed = (rf"(?:^|[，。；,;！!\s])(?:这单|这项任务|这件事|这份工作)?(?:请|麻烦)?(?:让|叫|(?:改派|转交|交接|改交|交|分配|转|派)给|换成|由)\s*{who}(?![a-zA-Z0-9_])"
                    rf"|(?:^|[，。；,;！!\s]){who}\s*(?:接手|接办|负责)"
                    rf"|\b(?:assign|reassign|hand|give)\b[^.?!\n]{{0,60}}?\bto\s+{who}\b|\bask\s+{who}\s+to\b")
        if re.search(directed, first, re.I):
            named.append(agent_id)
    attachments = _attachments(thread.id, started)
    return AssignmentContext(turn.id, thread.id, direct.event_key if direct else f"owner-turn-{turn.id}",
                             text, attachments, project, tuple(dict.fromkeys(ids)), tuple(named),
                             (started or {}).get("reference_epochs") or {}, cited, source.get("reference_event_key"), quoted_owner)


def _chat_tasks(owner, thread_id):
    return select(AgentJob.task_id).where(AgentJob.owner_id == owner, AgentJob.requester_thread_id == thread_id)


def owner_supplied_check(check, text):
    # A generated acceptance contract can contain filenames, command output or other private text.
    return all(value in (None, "", ".") or value in text
               for entry in [*check.get("commands", []), *check.get("files", [])]
               for key, value in entry.items() if key not in ("scope", "kind"))


async def steward_context(db, owner, turn):
    """Give the steward candidate IDs without forwarding another conversation's contents."""
    try:
        context = await resolve_assignment_context(db, owner, turn.id)
    except ValueError as exc:
        return str(exc)
    private = select(AgentJobEvent.id).join(AgentJob, AgentJobEvent.job_id == AgentJob.id).where(
        AgentJob.owner_id == owner, AgentJob.task_id == WorkItem.id, AgentJobEvent.kind == "feishu_assignment").exists()
    query = select(WorkItem.id, WorkItem.agent_id, FeatureScope.project,
                   case((private, None), else_=WorkItem.title).label("topic")).join(
        FeatureScope, FeatureScope.task_id == WorkItem.id).where(WorkItem.owner_id == owner)
    if context.reference_task_ids:
        query = query.where(WorkItem.id.in_(context.reference_task_ids))
    else:
        query = query.where(WorkItem.archived.is_(False))
        if context.project:
            query = query.where(steward_task_scope(owner, context.project, context.thread_id))
        else:
            query = query.where(WorkItem.id.in_(_chat_tasks(owner, turn.thread_id)))
        if context.named_agent_ids:
            query = query.where(WorkItem.agent_id.in_(context.named_agent_ids))
    rows = (await db.execute(query.order_by(WorkItem.id.desc()).limit(24))).all()
    if not rows:
        return ""
    import json
    return json.dumps({"referenced":list(context.reference_task_ids), "named_employees":list(context.named_agent_ids),
        "tasks":[{"id":r.id, "project":r.project, "employee":r.agent_id,
                  **({"topic":r.topic[:80]} if r.topic else {})} for r in rows]},ensure_ascii=False)


async def resolve_card(db, owner, context, target, args):
    """Explicit references beat a model's proposed card; ambiguous follow-ups return choices."""
    ids = list(context.reference_task_ids)
    if context.named_agent_ids and target not in context.named_agent_ids:
        raise ValueError(f"老板点名的员工编号是 {list(context.named_agent_ids)}，请交给指定员工。")
    claim = args.get("task_id")
    project = context.project or args.get("project")
    if claim is not None and (type(claim) is not int or claim < 1):
        raise ValueError("请使用工单上的正整数编号。")
    if project is not None and (not isinstance(project,str) or not project.strip() or len(project) > 160):
        raise ValueError("请提供有效的项目名称。")
    if args.get("task_title") is not None and not isinstance(args["task_title"],str):
        raise ValueError("请提供简短的任务标题。")
    transfer = bool(context.named_agent_ids and re.search(
        r"给错人|派错人|改派|转交给|交接给|改交给|换.{0,20}接手|\breassign\b", context.original_text.partition("\n")[0], re.I))
    query = select(WorkItem, FeatureScope).join(FeatureScope, FeatureScope.task_id == WorkItem.id).where(
        WorkItem.owner_id == owner)
    if not ids:
        if context.project:
            query = query.where(steward_task_scope(owner, context.project, context.thread_id))
        elif project:
            query = query.where(FeatureScope.project == project)
        if not context.project:
            query = query.where(WorkItem.id.in_(_chat_tasks(owner, context.thread_id)))
    if ids:
        rows = (await db.execute(query.where(WorkItem.id.in_(ids)))).all()
        if len(rows) > 1 and context.named_agent_ids:
            rows = [r for r in rows if r[0].agent_id in context.named_agent_ids]
    else:
        text = context.original_text.strip()
        continuing = transfer or args.get("assignment_intent") == "continue" or bool(re.search(
            r"^(?:请|麻烦)?(?:按.{0,12}(?:那个|上次|之前|原来)|继续|接着|续办|重新来|再试|还是上次)|原来的继续|\b(?:continue|resume)\b",text,re.I))
        if not continuing:
            if claim is None:
                if args.get("assignment_intent") == "independent" or not project:
                    return None
                from app.services.feature_tasks import normalized
                title = (args.get("task_title") or args.get("brief") or "").partition("\n")[0]
                candidates = (await db.execute(query.where(WorkItem.agent_id == target))).all()
                rows = [(card, scope) for card, scope in candidates
                        if normalized(title) in {normalized(label) for label in (card.title, *(scope.aliases or []))}
                        and (not args.get("version") or args["version"] == scope.version)
                        and (not args.get("done_when") or normalized(args["done_when"]) == normalized(scope.acceptance))]
                if not rows:
                    return None
            else:
                rows = (await db.execute(query.where(WorkItem.id == claim))).all()
                if not rows:
                    raise ValueError("所选工单不属于当前项目。")
        else:
            query = query.where(WorkItem.archived.is_(False))
            if context.named_agent_ids and not transfer:
                query = query.where(WorkItem.agent_id.in_(context.named_agent_ids))
            rows = (await db.execute(query.order_by(WorkItem.id))).all()
            topical = [r for r in rows if any(label and label.casefold() in text.casefold()
                       for label in [r[0].title, *(r[1].aliases or [])])]
            if topical:
                rows = topical
            if not rows:
                raise ValueError("没有找到可续办的工单，请确认项目或任务编号。")
    if len(rows) != 1:
        choices = "、".join(f"#{c.id} {c.title[:48]}" for c, _ in rows[:12])
        raise ValueError("请让老板选一项工单：" + choices)
    card, scope = rows[0]
    expected = context.reference_epochs.get(str(card.id))
    if expected is not None and expected != scope.assignment_epoch:
        raise ValueError(f"任务 #{card.id} 在这条消息送达后已改派，请按当前负责人核对。")
    if card.agent_id != target and target not in context.named_agent_ids:
        raise ValueError(f"任务 #{card.id} 由员工 {card.agent_id} 经办，请按原负责人续办。")
    if card.agent_id != target and len(context.named_agent_ids) != 1:
        raise ValueError(f"改派任务 #{card.id} 需要明确一位接手的员工。")
    if card.agent_id != target and not ids and not transfer:
        raise ValueError("改派前请确认原任务编号或引用原消息。")
    return card, scope


async def accepted_job(db, owner, requester_turn_id, task_id, target, context):
    return await db.scalar(select(AgentJob).join(FeatureScope, FeatureScope.task_id == AgentJob.task_id)
        .join(AgentJobEvent, AgentJobEvent.job_id == AgentJob.id).where(
            AgentJob.owner_id == owner, AgentJob.task_id == task_id, AgentJob.assignee_agent_id == target,
            AgentJob.assignment_epoch == FeatureScope.assignment_epoch, AgentJob.requester_turn_id == requester_turn_id,
            AgentJob.kind == "job", AgentJob.mode == "work", AgentJob.status.not_in(("failed", "cancelled")),
            AgentJobEvent.kind == "owner_request", AgentJobEvent.detail["turn_id"].as_integer() == context.turn_id)
        .order_by(AgentJob.id).limit(1))


async def _legacy_request(db, job):
    # Reconstruct old assignments from the owning turn or the steward's signed
    # intake ledger. A model's short origin_quote cannot replace either one.
    requester = await db.get(Thread, job.requester_thread_id)
    if not requester or requester.owner_id != job.owner_id or requester.agent_id != job.requester_agent_id:
        return None
    if requester.kind == "chat":
        root = await db.get(AgentJob, job.root_id) if job.root_id else None
        source = root.requester_turn_id if root and root.requester_thread_id == requester.id else job.requester_turn_id
        try:
            context = await resolve_assignment_context(db, job.owner_id, source)
        except ValueError:
            return None
        return {"turn_id":source, "text":context.original_text, "attachments":context.attachments}
    if requester.kind == "steward_ledger" and job.origin_ref:
        original = await db.get(FeatureMessage, (job.owner_id, job.origin_ref))
        if not original or original.result.get("job_id") != job.id or original.agent_id != job.assignee_agent_id:
            return None
        started = await db.scalar(select(Event.data).where(Event.turn_id == job.assignee_turn_id,
            Event.thread_id == job.assignee_thread_id, Event.type == "turn.started"))
        files = [{"thread_id":job.assignee_thread_id,"id":a["id"],"name":a.get("name",""),
                  "kind":a.get("kind","")} for a in (started or {}).get("attachments",[]) if a.get("id")]
        return {"turn_id":job.requester_turn_id,"text":original.text,"attachments":files}
    return None


async def employee_input(db, job):
    """Carry owner-submitted materials with the card, subject to the receiving employee's access."""
    if job.origin_kind == "routine" and (job.origin_ref or "").startswith("knowledge-week-"):
        return [], []
    from app.services.feature_tasks import private_handoff
    tenure = and_(AgentJob.assignee_agent_id == job.assignee_agent_id,
                  AgentJob.assignment_epoch == job.assignment_epoch)
    if job.assignment_epoch > 1 and not await private_handoff(db, job.task_id):
        tenure = or_(tenure, AgentJob.assignment_epoch < job.assignment_epoch)
    rows = await db.execute(select(AgentJob,AgentJobEvent.detail).outerjoin(AgentJobEvent,
        and_(AgentJob.id==AgentJobEvent.job_id, AgentJobEvent.kind=="owner_request"))
        .where(AgentJob.owner_id==job.owner_id, AgentJob.task_id==job.task_id, tenure,
               AgentJob.id<=job.id, AgentJob.kind=="job", AgentJob.mode=="work").order_by(AgentJob.id))
    messages, files, seen_turns = [], {}, set()
    for earlier, row in rows:
        if earlier.origin_kind == "routine" and (earlier.origin_ref or "").startswith("knowledge-week-"):
            continue
        inherited = earlier.assignment_epoch < job.assignment_epoch
        # Older ledger attachments live in the former employee's private thread.
        # Only replay a request independently traced to the owner's steward chat.
        if inherited:
            try:
                await _owner_chat(db, job.owner_id, earlier.requester_thread_id)
            except ValueError:
                continue
        if not row:
            row = await _legacy_request(db, earlier)
        if not row:
            continue
        for request in (row.get("quoted_owner"), row):
            if not request or request["turn_id"] in seen_turns:
                continue
            seen_turns.add(request["turn_id"])
            messages.append({"turn_id":request["turn_id"],"text":request["text"]})
            for attachment in request.get("attachments", []):
                if inherited and attachment["thread_id"] != earlier.requester_thread_id:
                    continue
                files[(attachment["thread_id"],attachment["id"])] = attachment
    return messages, list(files.values())


async def handoff_progress(db, job):
    """Summarize historical side effects. Expose file names only under a live read grant."""
    if job.assignment_epoch <= 1:
        return None
    import asyncio
    from pathlib import Path
    from app.services import feature_files, feature_tasks
    from app.team.feishu_handoff import previous_writes
    from app.team.work_verification import _content_digest
    worker = await db.get(Agent,job.assignee_agent_id)
    older = list(await db.scalars(select(AgentJob).where(AgentJob.owner_id==job.owner_id,
        AgentJob.task_id==job.task_id, AgentJob.kind=="job", AgentJob.mode=="work",
        AgentJob.assignment_epoch<job.assignment_epoch).order_by(AgentJob.id)))
    writes = await previous_writes(db, job)
    summary = {"previous_jobs":[], "file_receipts":0, "accessible_files":[],
               "remote_write_receipts":sum(s.confirmed for s in writes),
               "remote_writes_to_reconcile":sum(s.pending + int(s.uncertain) for s in writes)}
    restricted = await feature_tasks.private_handoff(db, job.task_id)
    if restricted:
        summary["restricted_materials"] = "原派工的飞书材料及附件需要接手员工自己的授权。请从当前有权访问的来源重新取用。"
    for prior in older:
        prior_worker = await db.get(Agent,prior.assignee_agent_id)
        may_read = bool(not restricted and worker and prior_worker and worker.team_id and worker.team_id == prior_worker.team_id
            and worker.owner_id == prior_worker.owner_id == job.owner_id and not prior_worker.archived_at
            and prior_worker.role_path and worker.team_workspaces_readable)
        files = None
        if may_read:
            try:
                root = feature_files.checked(Path(prior_worker.role_path))
                await feature_tasks.check_employee_root(db, job.owner_id, prior_worker, root)
                files = feature_files.FeatureFiles(root/"任务"/str(job.task_id), writable=False)
            except (OSError, ValueError):
                pass
        summary["previous_jobs"].append({"job_id":prior.id,"assignment_epoch":prior.assignment_epoch,
                                        "status":prior.status,"started":bool(prior.assignee_turn_id)})
        if prior.assignee_turn_id:
            summary["previous_jobs"][-1]["turn_status"] = await db.scalar(select(Turn.status).where(Turn.id==prior.assignee_turn_id))
        verdict = await db.scalar(select(AgentJobEvent.detail).where(AgentJobEvent.job_id==prior.id,
            AgentJobEvent.kind=="completion_evidence").order_by(AgentJobEvent.id.desc()).limit(1))
        if verdict:
            summary["previous_jobs"][-1]["verification"] = verdict.get("state")
        turns = select(Turn.id).join(Thread,Thread.id==Turn.thread_id).join(
            FeatureSession,FeatureSession.thread_id==Thread.id).where(FeatureSession.task_id==job.task_id,
                FeatureSession.assignment_epoch==prior.assignment_epoch, Thread.owner_id==job.owner_id,
                Thread.agent_id==prior.assignee_agent_id, Turn.id==prior.assignee_turn_id)
        outputs = await db.scalars(select(Event.data).where(Event.turn_id.in_(turns), Event.type=="tool.completed"))
        for output in outputs:
            entries = [output["file_receipt"]] if output.get("file_receipt") else output.get("file_receipts",[])
            for receipt in entries:
                if receipt.get("operation") not in ("fileChange","write_text","write","update","add") or not receipt.get("path"):
                    continue
                summary["file_receipts"] += 1
                if not files or len(summary["accessible_files"])>=24:
                    continue
                try:
                    if receipt.get("root") and Path(receipt["root"]) != files.role:
                        continue
                    location = files.path(receipt.get("scope","task"),receipt["path"])
                    current = await asyncio.to_thread(_content_digest, location)
                    recorded = receipt.get("sha256")
                    summary["accessible_files"].append({"employee":prior_worker.id, "path":location.relative_to(files.role).as_posix(),
                        "recorded_sha256":recorded,"current_sha256":current,
                        "status":("verified" if recorded==current else "changed") if recorded else "read_back"})
                except (OSError, ValueError):
                    continue
    return summary
