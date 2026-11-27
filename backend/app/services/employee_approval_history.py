"""Read-only projections of the owner's current grants for settings and the steward."""
from sqlalchemy import select

from app.db.models import Agent, AgentJob, Approval, Thread, Turn
from app.db.models.permission_rule import PermissionRule
from app.services import employee_approval_rules
from app.team import fence


async def pending_for_jobs(db, owner_id, jobs):
    """Only the employee's still-running turn can own a live approval for this job."""
    ids = [job.id for job in jobs if job.assignee_turn_id and job.status not in ("failed", "cancelled", "completed")]
    if not ids:
        return {}
    rows = (await db.execute(select(AgentJob.id, Approval.id.label("request"), Approval.tool, Approval.summary)
        .join(Approval, (Approval.turn_id == AgentJob.assignee_turn_id) & (Approval.thread_id == AgentJob.assignee_thread_id))
        .join(Thread, Thread.id == Approval.thread_id).join(Turn, Turn.id == Approval.turn_id)
        .where(AgentJob.id.in_(ids), AgentJob.owner_id == owner_id, Thread.owner_id == owner_id,
               Thread.agent_id == AgentJob.assignee_agent_id, Turn.thread_id == Thread.id,
               Turn.status == "running", Approval.status == "pending")
        .order_by(Approval.created_at))).all()
    by_job = {}
    for row in rows:
        by_job.setdefault(row.id, []).append({"request_id": row.request, "tool": row.tool,
                                             "action": fence.line(row.summary or row.tool, 200)})
    return by_job


async def current(db, owner_id, *, team_id=None):
    query = select(Agent.id, Agent.name).where(Agent.owner_id == owner_id)
    if team_id is not None:
        query = query.where(Agent.team_id == team_id, Agent.archived_at.is_(None))
    names = dict((await db.execute(query)).all())
    rows = await db.scalars(select(PermissionRule).where(
        PermissionRule.user_id == owner_id,
        PermissionRule.bucket == employee_approval_rules.BUCKET,
        PermissionRule.decision == "allow",
    ).order_by(PermissionRule.id.desc()))
    result = []
    for row in rows:
        scope = employee_approval_rules.decode(row)
        if scope and (team_id is None or scope["employee_id"] in names):
            result.append({
                "id": row.id, "employee_id": scope["employee_id"],
                "task_id": scope.get("task_id"),
                "employee_name": names.get(scope["employee_id"]) or f'#{scope["employee_id"]}',
                "workspace": scope["project"], "cwd": scope["cwd"], "command": scope["command"],
                "match": "exact", "created_at": row.created_at,
            })
    return result


async def steward_memory(db, owner_id, team_id):
    """Refresh on recall so revocations and pending decisions never go stale."""
    active = await current(db, owner_id, team_id=team_id)
    rows = (await db.execute(select(Approval.id, Agent.name, Approval.tool, Approval.status,
                                    Approval.delivered_at, Approval.decided_at)
        .join(Thread, Thread.id == Approval.thread_id).join(Agent, Agent.id == Thread.agent_id)
        .where(Thread.owner_id == owner_id, Thread.kind == "feature", Agent.team_id == team_id,
               Agent.owner_id == owner_id)
        .order_by(Approval.created_at.desc()).limit(12))).all()
    return {
        "active_rule_count": len(active),
        "recent_active_rules": [{k: r[k] for k in ("id", "employee_name", "workspace", "task_id", "command")}
                                for r in active[:12]],
        "recent_decisions": [
            {"request_id": r.id, "employee": r.name, "tool": r.tool, "decision": r.status,
             "allow_claimed_for_delivery": bool(r.delivered_at)} for r in rows
        ],
    }
