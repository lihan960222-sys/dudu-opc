"""Fence remote writes across employee changes using host receipts, without forwarding private resources."""
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import sqlite3

from sqlalchemy import select

from app.db.models import AgentJob, AgentJobEvent
from app.db.transactions import begin_write
from app.feishu import owner_oauth
from app.team import service
from app.team.feishu_assignment import DELIVERY_STAGES, WRITE_TOOLS, recorded_assignment


STAGES = (*DELIVERY_STAGES, "feishu_handoff_reconciled")


@dataclass
class WriteState:
    job_id: int
    employee: int
    tool: str
    operation: str | None = None
    pending: int = 0
    uncertain: bool = False
    confirmed: bool = False


async def previous_writes(db, job):
    if job.assignment_epoch <= 1:
        return []
    results = await db.execute(select(AgentJob.id, AgentJob.assignee_agent_id, AgentJobEvent.kind, AgentJobEvent.detail)
        .join(AgentJobEvent, AgentJobEvent.job_id == AgentJob.id).where(AgentJob.owner_id == job.owner_id,
            AgentJob.task_id == job.task_id, AgentJob.kind == "job", AgentJob.mode == "work",
            AgentJob.assignment_epoch < job.assignment_epoch, AgentJobEvent.kind.in_(STAGES)).order_by(AgentJobEvent.id))
    states = {}
    for job_id, employee, kind, detail in results:
        tool = detail.get("invoked_tool") or detail.get("tool")
        if tool not in WRITE_TOOLS or detail.get("employee", employee) != employee:
            continue
        state = states.setdefault((job_id, tool), WriteState(job_id, employee, tool))
        operation = detail.get("operation")
        if operation and state.operation is None:
            state.operation = operation
        elif operation and state.operation != operation:
            # A journal receipt for one operation cannot account for a different one.
            state.operation = ""
        if kind == "feishu_tool_started":
            state.pending += 1
            continue
        if kind == "feishu_handoff_reconciled":
            state.confirmed, state.uncertain, state.pending = True, False, 0
            continue
        state.pending = max(0, state.pending - 1)
        if kind == "feishu_delivered" or (kind == "feishu_superseded" and detail.get("result")):
            state.confirmed, state.uncertain = True, False
        elif detail.get("category") != "invalid_input":
            # Even validation may fail on readback after the provider applied a change.
            state.uncertain = True
        if state.operation == "":
            state.uncertain = True
    return list(states.values())


def _journal_confirms(journal, cfg, state, source):
    identity = owner_oauth.employee_config(cfg, state.employee)
    app = identity["steward"]["app_id"] if identity.get("_employee_oauth") else None
    if not app or not state.operation:
        return False
    if state.tool == "feishu_create_document":
        return bool(journal.execute("select 1 from employee_document_jobs where app=? and employee=? and job=? and confirmed=1",
                                    (app,state.employee,state.operation)).fetchone())
    if state.tool == "feishu_delete_document" and source.get("event_key") and source.get("employee") == state.employee:
        return bool(journal.execute("select 1 from employee_document_deletions where app=? and employee=? and event=? and confirmed=1",
                                    (app,state.employee,source["event_key"])).fetchone())
    return False


def _distinct_instruction(tool, previous, current):
    """Only a resource or edit pinned to a fresh owner instruction can distinguish a new write."""
    def changed(field):
        return bool(previous.get(field) and current.get(field) and previous[field] != current[field])
    if tool == "feishu_create_document":
        return changed("title")
    if tool == "feishu_edit_document":
        return changed("target_url") or changed("body")
    if tool == "feishu_delete_document":
        return changed("target_url")
    return changed("contact") or changed("instructions") or (
        current.get("weekly") and previous.get("weekly") and previous["weekly"] != current["weekly"])


async def reserve(db, rt, run, scope, name, operation):
    """Serialize write admission with correction, and record the attempt before any network operation."""
    from app.services.feature_tasks import assignment_current
    from app.team.feishu_tools_bridge import grant, invocation_key
    from app.team.jobs import event
    await begin_write(db)
    team = await service.team_for(db, rt.owner_id, lock=True)
    job = await db.get(AgentJob, scope.job.id, with_for_update=True, populate_existing=True)
    fresh = await grant(db, rt.owner_id, run, team)
    if (not job or not fresh or fresh.job.id != job.id or name not in fresh.tools or scope.assignment != fresh.assignment
            or not await assignment_current(db, job, lock=True)):
        raise service.TeamError("outside_feishu_assignment", "飞书授权或当前派工已经变更。")
    states = await previous_writes(db,job)
    path = Path(fresh.config["_access_config_path"]).parent / fresh.config["state_dir"] / "channel.sqlite3"
    reconciled = unknown = repeated = False
    for previous in states:
        if not (previous.pending or previous.uncertain or (previous.confirmed and previous.tool == name)):
            continue
        source = await recorded_assignment(db, previous.job_id) or {}
        if source.get("event_key"):
            from app.team.feishu_assignment import for_job
            try:
                source = await for_job(db, previous.job_id) or source
            except (OSError, ValueError, KeyError, sqlite3.Error):
                pass
        if previous.pending or previous.uncertain:
            if previous.operation is None:
                old_job = await db.get(AgentJob, previous.job_id)
                previous.operation = await invocation_key(db, old_job, source)
            try:
                with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as journal:
                    confirmed = _journal_confirms(journal, fresh.config, previous, source)
            except (OSError, ValueError, KeyError, sqlite3.Error):
                confirmed = False
            if not confirmed:
                unknown = True
                continue
            db.add(AgentJobEvent(job_id=previous.job_id,kind="feishu_handoff_reconciled",
                detail={"employee":previous.employee,"tool":previous.tool,"operation":previous.operation}))
            reconciled = previous.confirmed = True
        if previous.confirmed and previous.tool == name and not _distinct_instruction(name, source, fresh.assignment):
            repeated = True
    if unknown or repeated:
        if reconciled:
            await db.commit()
        if unknown:
            raise service.TeamError("previous_write_unknown", "前任的飞书写入结果尚未核对。请先用当前已获准的只读工具查看，管家核实原操作前暂不再次写入。")
        raise service.TeamError("previous_write_confirmed", "原派工已有这项写入的确认回执。请按当前员工的授权核对成品。另有修改或新文档时，用老板指定的内容或标题派工。")
    event(db, job, "feishu_tool_started", {"employee":run.agent_id, "tool":name, "operation":operation,
                                          "epoch":job.assignment_epoch,"turn_id":run.turn_id})
    await db.commit()
    return fresh
