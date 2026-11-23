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
                acceptance="", reference_id=None, intent="deliver", aliases=()):
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
        board = await work_planner.default_board(db, owner_id, commit=False)
        column = await db.scalar(select(WorkColumn).where(WorkColumn.board_id == board.id, WorkColumn.key == "ready"))
        card = WorkItem(owner_id=owner_id, agent_id=agent_id, board_id=board.id, column_id=column.id,
                        title=title, description=text, position=await work_planner.next_position(db, board.id, column.id))
        db.add(card)
        await db.flush()
        db.add(FeatureScope(task_id=card.id, project=project, aliases=list(aliases), version=version,
                            acceptance=acceptance, related_id=reference_id))
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
                and previous.workspace_path == workspace_path and previous.job_id == job_id):
            return previous, False
        last = await db.scalar(select(Turn).where(Turn.thread_id == previous.id,
            Turn.status != "running").order_by(Turn.seq.desc()).limit(1))
        if last:
            scope = await db.get(FeatureScope, task_id)
            scope.checkpoint = f"Turn {last.id} ({last.status})\n{last.prompt_text[:2000]}\n{(last.result or last.error or '')[:6000]}"
    thread = await thread_store.create_thread(db, owner_id=owner_id, agent_id=item.agent_id, provider="codex",
        title=item.title, kind="feature", workspace_path=workspace_path, model=model, job_id=job_id, commit=False)
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
    fields = dict(owner_id=owner_id, requester_agent_id=lead.id, requester_thread_id=ledger.id,
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
    if job.status != "dispatched":
        raise DispatchConflict("The dispatch is no longer active.")
    if reserve:
        receipt.result = {**receipt.result, "submission_sha256":digest}
    return None
