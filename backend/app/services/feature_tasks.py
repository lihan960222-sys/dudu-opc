"""Atomic intake for scoped planner cards. Callers commit with their delivery receipt."""
import unicodedata

from sqlalchemy import select
from app.db.models import Agent, AgentJob, Thread, Turn, WorkColumn, WorkItem
from app.db.models.feature import FeatureMessage, FeatureScope, FeatureSession
from app.db.transactions import begin_write
from app.services import thread_store, work_events, work_planner


def normalized(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


async def route(db, *, owner_id, agent_id, project, event_key, title, text, version="",
                acceptance="", reference_id=None, intent="deliver", aliases=(), dependencies=(), deliverables=(), priority="normal"):
    if not all(isinstance(s, str) and s.strip() for s in (owner_id, project, event_key)):
        raise ValueError("Owner, project and message identity are required")
    if intent not in {"query", "append", "deliver", "independent"}:
        raise ValueError("Unknown routing intent")
    await begin_write(db)
    agent = await db.scalar(select(Agent).where(Agent.id == agent_id, Agent.owner_id == owner_id,
                                                Agent.archived_at.is_(None)).with_for_update())
    if agent is None:
        raise ValueError("Unknown specialist")
    receipt = await db.get(FeatureMessage, (owner_id, event_key))
    if receipt is not None:
        if (receipt.agent_id, receipt.project) != (agent_id, project):
            raise ValueError("Message belongs to another scope")
        return receipt.result
    query = select(WorkItem, FeatureScope).join(FeatureScope, FeatureScope.task_id == WorkItem.id).where(
        WorkItem.owner_id == owner_id, WorkItem.agent_id == agent_id, FeatureScope.project == project)
    rows = (await db.execute(query.order_by(WorkItem.id))).all()
    names = {normalized(s) for s in (title, *aliases) if s.strip()}
    matches = []
    for card, scope in rows:
        if reference_id is not None:
            if card.id == reference_id:
                matches.append(card)
        elif (not names or names.intersection(normalized(s) for s in [card.title, *scope.aliases])) and (
            not version or version == scope.version
        ) and (not acceptance or normalized(acceptance) == normalized(scope.acceptance)):
            matches.append(card)
    if reference_id is not None and not matches:
        raise ValueError("Task reference outside project or specialist")
    card = None
    if intent == "independent" or (intent == "deliver" and not matches and names):
        if not title.strip():
            raise ValueError("A new delivery requires a title")
        dependency_ids, outputs = await validate_requirements(db, owner_id, project, dependencies, deliverables)
        if priority not in {"low", "normal", "high", "urgent"}:
            raise ValueError("Unknown task priority")
        board = await work_planner.default_board(db, owner_id, commit=False)
        column = await db.scalar(select(WorkColumn).where(WorkColumn.board_id == board.id, WorkColumn.key == "ready"))
        card = WorkItem(owner_id=owner_id, agent_id=agent_id, board_id=board.id, column_id=column.id,
                        title=title, description=text, priority=priority, position=await work_planner.next_position(db, board.id, column.id))
        db.add(card)
        await db.flush()
        db.add(FeatureScope(task_id=card.id, project=project, aliases=list(aliases), version=version,
                            acceptance=acceptance, related_id=reference_id, dependencies=dependency_ids, deliverables=outputs))
        action = "created"
    elif len(matches) == 1:
        card = matches[0]
        action = "query" if intent == "query" else "continued"
        if action == "continued" and (card.completed_at or card.archived):
            card.column_id = await db.scalar(select(WorkColumn.id).where(
                WorkColumn.board_id == card.board_id, WorkColumn.key == "ready"))
            card.archived, card.completed_at = False, None
            card.revision += 1
    else:
        action = "clarify"
    result = {"action": action, "task_id": card.id if card else None,
              "candidates": [c.id for c in matches] if action == "clarify" else []}
    if card and intent == "query":
        result["status"] = (await db.get(WorkColumn, card.column_id)).key
    elif card:
        work_events.add(db, card, action, detail=text)
    db.add(FeatureMessage(owner_id=owner_id, project=project, event_key=event_key,
                          agent_id=agent_id, text=text, result=result))
    await db.flush()
    return result


async def session_for(db, owner_id, task_id, *, model, workspace_path, job_id=None):
    await begin_write(db)
    item = await db.scalar(select(WorkItem).join(FeatureScope, FeatureScope.task_id == WorkItem.id).where(
        WorkItem.id == task_id, WorkItem.owner_id == owner_id).with_for_update())
    if item is None or item.agent_id is None:
        raise ValueError("Unknown assigned feature")
    if job_id is not None:
        job = await db.get(AgentJob, job_id)
        receipt = await db.get(FeatureMessage, (owner_id, job.origin_ref)) if job and job.origin_ref else None
        if (not receipt or receipt.result.get("task_id") != task_id or receipt.result.get("job_id") != job_id
                or (job.owner_id, job.assignee_agent_id) != (owner_id, item.agent_id)):
            raise ValueError("Job belongs to another specialist")
    previous = await db.scalar(select(Thread).join(FeatureSession, FeatureSession.thread_id == Thread.id)
                               .where(FeatureSession.task_id == task_id).order_by(Thread.id.desc()).limit(1))
    if previous:
        if (previous.agent_id, previous.owner_id) != (item.agent_id, owner_id):
            raise ValueError("Task assignment changed")
        if await thread_store.running_turn_id(db, previous.id):
            return previous, True
        if (not previous.archived_at and previous.session_state != "broken" and previous.model == model
                and previous.workspace_path == workspace_path):
            # Older task sessions could carry the creating job_id; each dispatch now
            # owns its individual Turn while the stable session belongs to the task.
            if previous.job_id is not None:
                previous.job_id = None
                await db.flush()
            return previous, False
        last = await db.scalar(select(Turn).where(Turn.thread_id == previous.id,
            Turn.status != "running").order_by(Turn.seq.desc()).limit(1))
        if last:
            scope = await db.get(FeatureScope, task_id)
            scope.checkpoint = f"Turn {last.id} ({last.status})\n{last.prompt_text[:2000]}\n{(last.result or last.error or '')[:6000]}"
    thread = await thread_store.create_thread(db, owner_id=owner_id, agent_id=item.agent_id, provider="codex",
        title=item.title, kind="feature", workspace_path=workspace_path, model=model, commit=False)
    db.add(FeatureSession(task_id=task_id, thread_id=thread.id, previous_id=previous.id if previous else None))
    item.thread_id = thread.id
    await db.flush()
    return thread, False


async def context_for(db, owner_id, agent, thread):
    from pathlib import Path
    from app.services.role_workspace import checked_root
    row = (await db.execute(select(WorkItem, FeatureScope, FeatureSession)
        .join(FeatureScope, FeatureScope.task_id == WorkItem.id)
        .join(FeatureSession, FeatureSession.task_id == WorkItem.id)
        .where(WorkItem.owner_id == owner_id, WorkItem.agent_id == agent.id,
               WorkItem.thread_id == thread.id, FeatureSession.thread_id == thread.id))).one_or_none()
    if row is None or (agent.owner_id, thread.owner_id, thread.agent_id) != (owner_id, owner_id, agent.id):
        raise ValueError("This thread is no longer the current execution session for its task.")
    item, scope, binding = row
    root = checked_root(Path(agent.role_path))
    task_dir = checked_root(root / "任务" / str(item.id))
    if Path(thread.workspace_path) != task_dir:
        raise ValueError("Thread workspace differs from the task directory.")
    parts = [f"Task #{item.id}: {item.title}", item.description,
             f"Requested deliverables: {scope.deliverables or []}",
             f"Version: {scope.version}", f"Acceptance: {scope.acceptance}"]
    facts = confirmed_facts(root, scope.version)
    if facts:
        parts.append("Owner-confirmed domain facts:\n" + "\n".join(facts))
    if binding.previous_id and scope.checkpoint:
        parts.append(f"Previous checkpoint: {scope.checkpoint}")
    return "\n".join(parts) + "\n\n"


async def dispatch(db, *, owner_id, steward_id, task_id, event_key, thread_id):
    """Record the steward's deterministic hand-off after authenticated DM intake."""
    import hashlib
    from sqlalchemy import func
    from app.team import service
    from app.db.models import AgentJobEvent
    await begin_write(db)
    team = await service.team_for(db, owner_id, lock=True)
    if not team or not team.lead_agent_id or (steward_id is not None and steward_id != team.lead_agent_id):
        raise ValueError("Configure this owner's steward as the current team lead")
    lead = await service.member(db, owner_id, team.lead_agent_id, team_id=team.id)
    receipt = await db.get(FeatureMessage, (owner_id, event_key), with_for_update=True)
    session = await db.get(FeatureSession, thread_id)
    item = await db.get(WorkItem, task_id)
    if (not receipt or receipt.result.get("task_id") != task_id or not session or session.task_id != task_id
            or not item or item.owner_id != owner_id or item.thread_id != thread_id
            or receipt.agent_id != item.agent_id or receipt.agent_id == lead.id):
        raise ValueError("Dispatch does not belong to this specialist task")
    await service.member(db, owner_id, receipt.agent_id, team_id=team.id)
    if receipt.result.get("job_id"):
        return receipt.result["job_id"]
    scope = await db.get(FeatureScope, task_id)
    if scope.dependencies:
        completed = set(await db.scalars(select(WorkItem.id).where(WorkItem.owner_id == owner_id,
            WorkItem.id.in_(scope.dependencies), WorkItem.completed_at.is_not(None))))
        if not completed.issuperset(scope.dependencies):
            raise ValueError("A prerequisite task is still awaiting acceptance.")
    ledger = await db.scalar(select(Thread).where(Thread.owner_id == owner_id, Thread.agent_id == lead.id,
                                                   Thread.kind == "steward_ledger").limit(1))
    if not ledger:
        ledger = await thread_store.create_thread(db, owner_id=owner_id, agent_id=lead.id, provider="codex",
            kind="steward_ledger", title="管家派单记录", workspace_path=lead.role_path, commit=False)
    seq = await db.scalar(select(func.max(Turn.seq)).where(Turn.thread_id == ledger.id))
    request = Turn(thread_id=ledger.id, seq=(seq or 0) + 1, prompt_text=receipt.text, status="succeeded",
                   result=f"派给任务 #{task_id}", origin="person", finished_at=service.now())
    db.add(request)
    await db.flush()
    fields = dict(task_id=task_id, project=receipt.project, owner_id=owner_id, requester_agent_id=lead.id, requester_thread_id=ledger.id,
                  requester_turn_id=request.id, origin_kind="person", origin_ref=event_key,
                  brief=receipt.text, brief_hash=hashlib.sha256(receipt.text.encode()).hexdigest(),
                  status="dispatched", reply="wait", started_at=service.now())
    holder = AgentJob(**fields, kind="root", assignee_agent_id=lead.id, assignee_thread_id=ledger.id)
    db.add(holder)
    await db.flush()
    job = AgentJob(**fields, root_id=holder.id, parent_id=holder.id, depth=1,
                   assignee_agent_id=item.agent_id, assignee_thread_id=thread_id)
    db.add(job)
    await db.flush()
    receipt.result = {**receipt.result, "job_id": job.id}
    db.add(AgentJobEvent(job_id=job.id, kind="dispatched", detail={"task_id":task_id}))
    await thread_store.add_event_in_transaction(db, ledger.id, "steward.dispatched",
        {"task_id": task_id, "job_id":job.id, "agent_id":item.agent_id}, turn_id=request.id)
    return job.id


async def bind_turn(db, owner_id, event_key, thread_id, turn_id):
    await begin_write(db)
    message = await db.get(FeatureMessage, (owner_id, event_key))
    job = await db.get(AgentJob, message.result["job_id"], with_for_update=True)
    turn = await db.get(Turn, turn_id)
    if (not turn or turn.thread_id != thread_id or job.assignee_thread_id != thread_id or
            job.owner_id != owner_id or job.assignee_turn_id not in (None, turn_id)):
        raise ValueError("Turn does not belong to this dispatch")
    if job.assignee_turn_id == turn_id:
        return
    await db.get(Thread, thread_id, with_for_update=True)
    assigned = await db.scalar(select(AgentJob.id).where(AgentJob.assignee_turn_id == turn_id,
        AgentJob.id != job.id, AgentJob.origin_ref.is_not(None)).limit(1))
    if assigned:
        raise ValueError("This turn already belongs to another dispatch")
    job.assignee_turn_id = turn_id
    item = await db.get(WorkItem, message.result["task_id"], with_for_update=True)
    item.column_id = await db.scalar(select(WorkColumn.id).where(WorkColumn.board_id == item.board_id,
                                                                 WorkColumn.key == "doing"))
    item.revision += 1


async def report(db, owner_id, event_key, thread_id, turn_id):
    from app.db.models import AgentJobEvent, AgentJobInbox
    from app.team.service import now
    await begin_write(db)
    msg = await db.get(FeatureMessage, (owner_id, event_key))
    job = await db.get(AgentJob, msg.result["job_id"], with_for_update=True)
    turn = await db.get(Turn, turn_id)
    binding = await db.get(FeatureSession, thread_id)
    if (not turn or turn.thread_id != thread_id or job.assignee_turn_id != turn_id
            or job.assignee_thread_id != thread_id or job.owner_id != owner_id
            or not binding or binding.task_id != msg.result["task_id"] or turn.status == "running"):
        raise ValueError("Report does not belong to a finished turn of this dispatch")
    if job.status != "dispatched":
        return
    parent = await db.get(AgentJob, job.parent_id, with_for_update=True)
    if (parent.owner_id != owner_id or parent.assignee_agent_id != job.requester_agent_id
            or parent.assignee_thread_id != job.requester_thread_id or parent.id != job.root_id):
        raise ValueError("Invalid parent job")
    status = "completed" if turn.status == "succeeded" else "cancelled" if turn.status == "cancelled" else "failed"
    item = await db.get(WorkItem, binding.task_id, with_for_update=True)
    latest = await db.scalar(select(Turn.id).where(Turn.thread_id == thread_id).order_by(Turn.seq.desc()).limit(1))
    if item.thread_id == thread_id and latest == turn.id and not item.completed_at and not item.archived:
        key = "review" if status == "completed" else "waiting"
        item.column_id = await db.scalar(select(WorkColumn.id).where(WorkColumn.board_id == item.board_id,
                                                                     WorkColumn.key == key))
        item.revision += 1
    job.result = parent.result = turn.result or turn.error or turn.status
    job.status = parent.status = status
    job.finished_at = parent.finished_at = now()
    db.add(AgentJobInbox(owner_id=owner_id, holder_id=parent.id, job_id=job.id, kind=status))
    db.add(AgentJobEvent(job_id=parent.id, kind="specialist_report",
                         detail={"job_id": job.id, "turn_id": turn_id, "status":status}))
    await thread_store.add_event_in_transaction(db, parent.assignee_thread_id, "steward.report",
        {"task_id":binding.task_id, "job_id":job.id, "turn_id":turn_id, "status":status},
        turn_id=parent.requester_turn_id)


def confirmed_facts(root, version):
    """The role owner curates this UTF-8 JSON file; feature tools have read-only access."""
    import json
    import os
    import stat
    from app.services.role_workspace import checked_root, linked
    folder = checked_root(root) / "资料"
    path = folder / "领域事实.json"
    if not path.exists():
        return []
    checked_root(folder)
    info = path.lstat()
    if linked(path) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError(f"Expected an unlinked role facts file: {path}")
    with path.open("rb") as stream:
        if not os.path.samestat(info, os.fstat(stream.fileno())) or path.resolve(strict=True) != path:
            raise ValueError(f"Role facts file changed during opening: {path}")
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError(f"Role facts file exceeds 64 KiB: {path}")
    try:
        records = json.loads(raw.decode("utf-8-sig"))
        required = ("fact", "source", "version")
        if not isinstance(records, list) or any(not isinstance(record, dict) or
            any(not isinstance(record.get(k), str) or not record[k].strip() for k in required) for record in records):
            raise ValueError
    except (UnicodeError, ValueError) as exc:
        raise ValueError(f"{path} must contain a list of facts with fact, source and version") from exc
    return [f"{r['fact']} (source: {r['source']}, version: {r['version']})"
            for r in records if r["version"] in ("*", version)]


class DispatchConflict(ValueError):
    pass


def direct_input_hash(prompt, attachment_ids, skills):
    import hashlib
    import json
    return hashlib.sha256(json.dumps([prompt,attachment_ids or [],skills or []],ensure_ascii=False).encode()).hexdigest()


async def direct_submission(db, thread, key, digest, *, lock=False):
    from app.db.models import DirectTurnReceipt
    from app.team import service
    # Serialize this owner's arrivals across *different* chats, too.
    team = await service.team_for(db,thread.owner_id,lock=lock)
    if thread.kind != "chat" or not team or not thread.agent_id or thread.agent_id != team.lead_agent_id:
        raise DispatchConflict("Direct messages require the current steward's chat.")
    receipt = await db.get(DirectTurnReceipt,(thread.owner_id,key))
    if not receipt:
        return None
    if receipt.thread_id != thread.id or digest != receipt.input_sha256:
        raise DispatchConflict("The direct message is already bound to a different chat or input.")
    prior = await db.get(Turn,receipt.turn_id)
    if not prior or prior.thread_id != thread.id:
        raise DispatchConflict("The original turn is no longer available.")
    return prior


async def submission(db, thread, key, prompt, attachment_ids, *, reserve=False):
    """Return the committed original turn, or reserve its exact input in the start transaction."""
    import hashlib
    import json
    receipt = await db.get(FeatureMessage, (thread.owner_id, key), with_for_update=reserve)
    binding = await db.get(FeatureSession, thread.id)
    if not receipt or not binding or receipt.result.get("task_id") != binding.task_id:
        raise DispatchConflict("Dispatch belongs to a different task.")
    job_id = receipt.result.get("job_id")
    job = await db.get(AgentJob, job_id, with_for_update=reserve) if job_id else None
    if (not job or job.origin_ref != key or job.owner_id != thread.owner_id
            or job.assignee_agent_id != thread.agent_id or job.assignee_thread_id != thread.id):
        raise DispatchConflict("Dispatch belongs to a different session.")
    digest = hashlib.sha256(json.dumps([prompt, list(dict.fromkeys(attachment_ids))], ensure_ascii=False).encode()).hexdigest()
    saved = receipt.result.get("submission_sha256")
    if saved and saved != digest:
        raise DispatchConflict("The dispatch was already submitted with different input.")
    if job.assignee_turn_id is not None:
        turn = await db.get(Turn, job.assignee_turn_id)
        if not turn or turn.thread_id != thread.id or not saved:
            raise DispatchConflict("Legacy dispatch needs reconciliation.")
        return turn
    if job.status != "dispatched" and not (job.reply == "resume" and job.status == "running"):
        raise DispatchConflict("The dispatch is no longer active.")
    if reserve:
        receipt.result = {**receipt.result, "submission_sha256":digest}
    return None


async def consult_scope(db, owner_id, team, caller, turn, thread):
    """Derive the sender, card and project from the live turn, never from model arguments."""
    from app.team.service import TeamError
    if (not thread or thread.kind != "feature" or caller.id == team.lead_agent_id or not caller.delegation_enabled
            or thread.owner_id != owner_id or thread.agent_id != caller.id or caller.team_id != team.id
            or turn.thread_id != thread.id or turn.status != "running"):
        raise TeamError("consult_scope", "This turn is not assigned to the specialist.")
    row = (await db.execute(select(FeatureScope, AgentJob)
        .join(WorkItem, WorkItem.id == FeatureScope.task_id)
        .join(AgentJob, AgentJob.task_id == WorkItem.id)
        .where(WorkItem.owner_id == owner_id, WorkItem.thread_id == thread.id, WorkItem.agent_id == caller.id,
               WorkItem.archived.is_(False), WorkItem.completed_at.is_(None),
               AgentJob.owner_id == owner_id, AgentJob.assignee_turn_id == turn.id,
               AgentJob.assignee_thread_id == thread.id, AgentJob.assignee_agent_id == caller.id,
               AgentJob.requester_agent_id == team.lead_agent_id, AgentJob.status.in_(("dispatched", "running")),
               AgentJob.project == FeatureScope.project))).one_or_none()
    if row is None:
        raise TeamError("consult_scope", "No active steward dispatch was found for this task.")
    scope, job = row
    assigned_agents = (
        select(WorkItem.agent_id)
        .join(FeatureScope, FeatureScope.task_id == WorkItem.id)
        .where(WorkItem.owner_id == owner_id, WorkItem.archived.is_(False),
               WorkItem.completed_at.is_(None), FeatureScope.project == scope.project)
    )
    peers = list(await db.scalars(
        select(Agent).where(
            Agent.id.in_(assigned_agents), Agent.owner_id == owner_id, Agent.team_id == team.id,
            Agent.archived_at.is_(None), Agent.provider == "codex", Agent.role_path.is_not(None),
            Agent.id.not_in([caller.id, team.lead_agent_id]),
        ).order_by(Agent.id)
    ))
    return job, peers


async def consult_parties(db, owner_id, job, *, verify_delivery=False):
    from app.team import service
    team = await service.team_for(db, owner_id)
    if not team or job.owner_id != owner_id or not job.parent_id or job.mode != "consult" or (not verify_delivery and job.status != "running"):
        raise ValueError("No active project consultation.")
    caller = await service.member(db, owner_id, job.requester_agent_id, team_id=team.id)
    target = await service.member(db, owner_id, job.assignee_agent_id, team_id=team.id)
    turn = await db.get(Turn, job.requester_turn_id)
    thread = await db.get(Thread, job.requester_thread_id)
    if not turn:
        raise ValueError("Requester turn has ended.")
    parent, peers = await consult_scope(db, owner_id, team, caller, turn, thread)
    if (job.parent_id != parent.id or job.task_id != parent.task_id or job.project != parent.project
            or target.id not in {peer.id for peer in peers}):
        raise ValueError("Consultation is outside the current project assignment.")
    return target


async def consult_thread(db, owner_id, job):
    from pathlib import Path
    from app.services.role_workspace import checked_root, load
    target = await consult_parties(db, owner_id, job)
    root = checked_root(Path(target.role_path))
    role = load(root, target.model)
    return await thread_store.create_thread(db, owner_id=owner_id, agent_id=target.id, provider="codex",
        model=role.model, workspace_path=str(root), kind="feature_consult", job_id=job.id,
        title=f"咨询 #{job.id} · {job.brief[:60]}")


async def consult_context(db, owner_id, agent, thread):
    from pathlib import Path
    from app.services.role_workspace import checked_root
    job = await db.get(AgentJob, thread.job_id)
    if not job or job.assignee_thread_id != thread.id or agent.id != job.assignee_agent_id:
        raise ValueError("Consultation thread does not belong to this employee.")
    target = await consult_parties(db, owner_id, job)
    root = checked_root(Path(target.role_path))
    if root != Path(thread.workspace_path):
        raise ValueError("Consultant's role directory changed.")
    scope = await db.get(FeatureScope, job.task_id)
    facts = confirmed_facts(root,scope.version)
    return "\n".join([f"Project {scope.project}, task #{scope.task_id}, version {scope.version}.", *facts])


async def validate_requirements(db, owner_id, project, dependencies, deliverables):
    if not isinstance(dependencies, (list, tuple)) or len(dependencies) > 32 or any(type(n) != int for n in dependencies):
        raise ValueError("Dependencies must be a list of up to 32 task IDs.")
    if not isinstance(deliverables, (list, tuple)) or len(deliverables) > 32 or any(
        not isinstance(s, str) or not s.strip() or len(s) > 500 for s in deliverables):
        raise ValueError("Supply up to 32 non-empty deliverable descriptions.")
    ids = list(dict.fromkeys(dependencies))
    if ids:
        allowed = set(await db.scalars(select(WorkItem.id).join(FeatureScope, FeatureScope.task_id == WorkItem.id)
            .where(WorkItem.id.in_(ids), WorkItem.owner_id == owner_id, FeatureScope.project == project)))
        if set(ids) != allowed:
            raise ValueError("Dependencies must belong to the same project.")
    return ids, [s.strip() for s in deliverables]


async def thread_handoff(db, owner_id, thread_id, *, task_id=None):
    """Owner-only native resume details for a current task binding or the steward's chat."""
    from uuid import UUID
    from app.team import service
    thread = await db.get(Thread, thread_id)
    if not thread or thread.owner_id != owner_id or thread.provider != "codex" or thread.session_state != "active":
        return None
    state = thread.provider_state or {}
    if thread.kind == "feature":
        binding = await db.get(FeatureSession, thread_id)
        item = await db.get(WorkItem, binding.task_id) if binding else None
        if (not item or item.owner_id != owner_id or item.agent_id != thread.agent_id or item.thread_id != thread.id
                or (task_id is not None and task_id != item.id)):
            return None
        cwd = thread.workspace_path
    elif thread.kind == "chat" and task_id is None:
        team = await service.team_for(db, owner_id)
        if not team or team.lead_agent_id != thread.agent_id:
            return None
        # Existing chats predate native_cwd; their immutable workspace was passed as cwd.
        cwd = state.get("native_cwd") or thread.workspace_path
    else:
        return None
    from app.core.config import settings
    if state.get("service_history") or settings.STEWARD_STRICT_OWNER == owner_id:
        return {"mode": "workbench", "thread_id": thread.id}
    native = state.get("codex_thread_id")
    try:
        uuid = str(UUID(native)) if isinstance(native,str) else None
    except ValueError:
        return None
    if not uuid or uuid != native or not cwd:
        return None
    if state.get("steward_deepseek"):
        # Only the backend provisions the per-turn team mailbox, approvals and
        # dedicated provider credential for this steward's session.
        if thread.kind != "chat":
            return None
        return {"mode": "workbench", "thread_id": thread.id}
    handoff = {"session_id":uuid, "cwd":cwd, "resume_command":f"codex resume {uuid}"}
    if activity := state.get("last_native_activity"):
        handoff["last_native_activity"] = activity
    return handoff


async def protocol(db, owner_id, task_id, *, owner_handoff=False):
    row = (await db.execute(select(WorkItem, FeatureScope, WorkColumn.key)
        .join(FeatureScope, FeatureScope.task_id == WorkItem.id).join(WorkColumn, WorkColumn.id == WorkItem.column_id)
        .where(WorkItem.owner_id == owner_id, WorkItem.id == task_id))).one_or_none()
    if not row:
        return None
    item, scope, status = row
    lead = await db.scalar(select(AgentJob.requester_agent_id).where(AgentJob.owner_id == owner_id,
        AgentJob.task_id == task_id, AgentJob.kind == "job", AgentJob.mode == "work").order_by(AgentJob.id).limit(1))
    handoff = await thread_handoff(db,owner_id,item.thread_id,task_id=item.id) if owner_handoff and item.thread_id else None
    return {"native_handoff":handoff, "revision":item.revision,
        "task_id": item.id, "project": scope.project, "from": lead, "to": item.agent_id,
        "objective": item.description or item.title, "priority": item.priority, "status": status,
        "dependencies": scope.dependencies or [], "deliverables": scope.deliverables or [],
        "acceptance_criteria": scope.acceptance.splitlines(),
        "version": scope.version, "related_task_id": scope.related_id, "thread_id":item.thread_id}


async def attach_command(db, job, args):
    """Attach a native lead delegate receipt to a durable planner card in the enqueue transaction."""
    key = f"team-job-{job.id}"
    title = args.get("task_title") or job.brief.splitlines()[0][:200]
    if not isinstance(title,str) or not title.strip() or len(title)>200:
        raise ValueError("A work assignment needs a short task title.")
    result = await route(db, owner_id=job.owner_id, agent_id=job.assignee_agent_id,
        project=args["project"].strip(), event_key=key, title=title, text=job.brief,
        version=args.get("version",""), acceptance=job.done_when or "", reference_id=args.get("task_id"),
        dependencies=args.get("dependencies",[]), deliverables=args.get("deliverables",[]),
        priority=args.get("priority","normal"))
    if not result["task_id"]:
        raise ValueError("Several task cards match. Supply the intended task_id.")
    previous = await db.scalar(select(AgentJob).where(AgentJob.owner_id == job.owner_id,
        AgentJob.task_id == result["task_id"], AgentJob.assignee_agent_id == job.assignee_agent_id,
        AgentJob.kind == "job", AgentJob.mode == "work", AgentJob.id != job.id)
        .order_by(AgentJob.id.desc()).limit(1))
    if previous and previous.status == "needs_input":
        if previous.question and previous.requester_thread_id == job.requester_thread_id:
            raise ValueError(f"Job {previous.id} is awaiting clarification. Use answer_job_question with "
                             f"job_id={previous.id}, question_turn_id={previous.assignee_turn_id}.")
        raise ValueError(f"Job {previous.id} is already waiting for a decision on this task.")
    message = await db.get(FeatureMessage,(job.owner_id,key))
    message.result = {**result,"job_id":job.id}
    job.origin_ref,job.task_id,job.project = key,result["task_id"],message.project


async def eligible_since(db, job):
    """Queue timeout starts after the last prerequisite is accepted."""
    scope = await db.get(FeatureScope, job.task_id)
    if not scope:
        return None
    if not scope.dependencies:
        return job.created_at
    dates = list(await db.scalars(select(WorkItem.completed_at).where(WorkItem.owner_id == job.owner_id,
        WorkItem.id.in_(scope.dependencies), WorkItem.completed_at.is_not(None))))
    if len(dates) != len(scope.dependencies):
        return None
    return max(job.created_at, *dates)


async def command_thread(db, owner_id, job):
    from pathlib import Path
    from app.services.role_workspace import checked_root, load
    from app.team import service
    await begin_write(db)
    team = await service.team_for(db, owner_id,lock=True)
    if not team or team.lead_agent_id != job.requester_agent_id or job.owner_id != owner_id:
        raise ValueError("The current steward no longer owns this dispatch.")
    agent = await service.member(db,owner_id,job.assignee_agent_id,team_id=team.id)
    root=checked_root(Path(agent.role_path))
    model=load(root, agent.model).model
    folder=root/"任务"/str(job.task_id)
    if folder.resolve() != folder:
        raise ValueError("Linked task directory refused.")
    folder.mkdir(parents=True, exist_ok=True)
    checked_root(folder)
    thread,busy=await session_for(db,owner_id,job.task_id,workspace_path=str(folder),model=model)
    if busy:
        raise thread_store.ThreadBusy(thread.id)
    await db.commit()
    return thread


async def set_command_state(db, job):
    item = await db.get(WorkItem,job.task_id,with_for_update=True)
    if not item or item.owner_id!=job.owner_id or item.completed_at or item.archived:
        return
    if item.thread_id and item.thread_id != job.assignee_thread_id:
        return
    if job.assignee_turn_id:
        latest=await db.scalar(select(Turn.id).where(Turn.thread_id==item.thread_id).order_by(Turn.seq.desc()).limit(1))
        if latest != job.assignee_turn_id:
            return
    column="review" if job.status=="completed" else "waiting"
    item.column_id=await db.scalar(select(WorkColumn.id).where(WorkColumn.board_id==item.board_id,WorkColumn.key==column))
    item.revision+=1


async def update_protocol(db,owner_id,task_id,revision,changes):
    from app.db.models import WorkBoard
    from app.team.service import TeamError
    await begin_write(db)
    # Serialize dependency graph changes for this owner's feature cards.
    await db.scalar(select(WorkBoard.id).where(WorkBoard.owner_id==owner_id,
                                               WorkBoard.key=="default").with_for_update())
    item=await db.get(WorkItem,task_id,with_for_update=True)
    scope=await db.get(FeatureScope,task_id)
    if not item or item.owner_id!=owner_id or not scope:
        raise TeamError("task_not_found","Project task not found.")
    if item.revision!=revision:
        raise TeamError("stale_revision","The task changed. Reload it before editing.")
    if not changes:
        raise ValueError("Supply a task requirement to update.")
    deps, outputs=await validate_requirements(db,owner_id,scope.project,
        changes.get("dependencies",scope.dependencies or []),changes.get("deliverables",scope.deliverables or []))
    if deps and deps!=scope.dependencies:
        graph=dict((await db.execute(select(FeatureScope.task_id,FeatureScope.dependencies)
            .join(WorkItem,WorkItem.id==FeatureScope.task_id)
            .where(WorkItem.owner_id==owner_id,FeatureScope.project==scope.project))).all())
        waiting,seen=list(deps),set()
        while waiting:
            dep=waiting.pop()
            if dep==task_id:
                raise ValueError("Task dependencies must not form a cycle.")
            if dep not in seen:
                seen.add(dep)
                waiting.extend(graph.get(dep) or [])
    priority=changes.get("priority",item.priority)
    if priority not in {"low","normal","high","urgent"}:
        raise ValueError("Unknown task priority.")
    criteria=changes.get("acceptance_criteria",scope.acceptance.splitlines())
    if not isinstance(criteria,list) or len(criteria)>32 or any(not isinstance(c,str) or not c.strip() or len(c)>2000 for c in criteria):
        raise ValueError("Supply up to 32 non-empty acceptance criteria.")
    item.priority=priority
    scope.dependencies,scope.deliverables,scope.acceptance=deps,outputs,"\n".join(criteria)
    item.revision+=1
    work_events.add(db,item,"requirements_updated",detail=", ".join(sorted(changes)))
    await db.commit()
    return await protocol(db,owner_id,task_id,owner_handoff=True)
