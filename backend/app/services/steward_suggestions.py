"""Local steward observations, review options, and exactly-once planning receipts."""
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from sqlalchemy import exists, func, or_, select

from app.core.steward_timing import PATROL_SECONDS
from app.db.models import Agent, AppSetting, FeatureScope, StewardSuggestion, WorkColumn, WorkItem
from app.db.transactions import begin_write
from app.services import work_events, work_planner
from app.team import service as teams


@dataclass(frozen=True)
class Signal:
    label: str
    brief: str
    followup: str


SIGNALS = {
    "waiting": Signal("有任务在等待", "列出阻塞和已有线索", "让管家跟进阻塞"),
    "review": Signal("有成果等待验收", "整理验收清单", "让管家安排复核"),
    "overdue": Signal("已过计划日期", "核对延期任务的现状", "让管家准备调整方案"),
    "criteria": Signal("交付条件尚未写清", "整理待确认的验收要点", "让管家准备验收草案"),
    "unassigned": Signal("有可开始的任务尚无负责人", "查看任务及派工依据", "让管家准备派工方案"),
}


def _tasks(owner):
    return (select(WorkItem, WorkColumn.key, FeatureScope)
            .join(WorkColumn, WorkColumn.id == WorkItem.column_id)
            .outerjoin(FeatureScope, FeatureScope.task_id == WorkItem.id)
            .where(WorkItem.owner_id == owner, WorkItem.archived.is_(False),
                   WorkItem.completed_at.is_(None), WorkColumn.key != "done"))


def _observation(item, column, scope, now):
    if column in ("waiting", "review"):
        kind = column
    elif _past_due(item, now):
        kind = "overdue"
    elif scope is not None and not scope.acceptance.strip():
        kind = "criteria"
    elif column == "ready" and item.agent_id is None and item.team_id is None:
        kind = "unassigned"
    else:
        return None
    signal = SIGNALS[kind]
    title = f"{signal.label} · {item.title}"
    parts = [f"任务 #{item.id}", f"项目 {scope.project}" if scope else "", (item.description or "")[:700],
             f"验收条件：{scope.acceptance[:350]}" if scope and scope.acceptance else "",
             f"计划日期：{item.due_at or item.due_date}" if item.due_date or item.due_at else ""]
    detail = "\n".join(p for p in parts if p)
    facts = [kind, title, detail, item.agent_id, item.team_id, item.priority, scope.version if scope else ""]
    key = hashlib.sha256(json.dumps(facts, ensure_ascii=False).encode()).hexdigest()
    return dict(kind=kind, title=title, detail=detail, source_key=key)


def _past_due(item, now):
    if item.due_at:
        return item.due_at <= now
    if item.due_date:
        try:
            zone = ZoneInfo(item.timezone or "UTC")
        except (ZoneInfoNotFoundError, ValueError):
            zone = UTC
        return item.due_date < now.astimezone(zone).date()
    return False


async def scan(db, owner, *, now=None):
    """Run one due local review atomically; return the seconds until the next check."""
    await begin_write(db)
    team = await teams.team_for(db, owner, lock=True)
    if not team or not team.lead_agent_id:
        await db.commit()
        return 120  # The owner may still be setting up the team.
    now = now or datetime.now(UTC)
    key = f"internal.steward.patrol.{owner}"
    checkpoint = await db.get(AppSetting, key)
    if checkpoint is None:
        # Older installs recorded suggestions, but not empty patrols.
        latest = await db.scalar(select(func.max(StewardSuggestion.created_at)).where(
            StewardSuggestion.owner_id == owner))
        previous = latest.timestamp() if latest else None
        checkpoint = AppSetting(key=key, value_json=previous, version=1, updated_by="steward")
        db.add(checkpoint)
    else:
        previous = checkpoint.value_json
    if previous is not None:
        remaining = previous + PATROL_SECONDS - now.timestamp()
        if remaining > 0:
            await db.commit()
            return remaining
    checkpoint.value_json = now.timestamp()
    pending = {r.task_id: r for r in await db.scalars(select(StewardSuggestion).where(
        StewardSuggestion.owner_id == owner, StewardSuggestion.state == "pending"))}
    is_followup = exists().where(StewardSuggestion.followup_task_id == WorkItem.id,
                                 StewardSuggestion.owner_id == owner)
    query = _tasks(owner).where(~is_followup, or_(
        WorkColumn.key.in_(("waiting", "review", "ready")), WorkItem.due_at.is_not(None),
        WorkItem.due_date.is_not(None), FeatureScope.acceptance == ""))
    for item, column, scope in await db.execute(query):
        facts = _observation(item, column, scope, now)
        if not facts:
            continue
        old = pending.get(item.id)
        if old and old.source_key == facts["source_key"]:
            pending.pop(item.id)
            continue
        seen = await db.scalar(select(StewardSuggestion.id).where(StewardSuggestion.owner_id == owner,
            StewardSuggestion.task_id == item.id, StewardSuggestion.source_key == facts["source_key"]))
        if seen is None:
            db.add(StewardSuggestion(owner_id=owner, task_id=item.id, **facts))
    for old in pending.values():
        old.state = "expired"
        old.revision += 1
    await db.commit()
    return PATROL_SECONDS


def public(row):
    rule = SIGNALS[row.kind]
    return {"id":row.id, "task_id":row.task_id, "revision":row.revision, "state":row.state, "title":row.title,
            "detail":row.detail, "options":[{"id":"brief","label":rule.brief},{"id":"followup","label":rule.followup}],
            "option":row.option, "report":row.report, "followup_task_id":row.followup_task_id}


async def owned(db, owner, ident, *, lock=False):
    query = select(StewardSuggestion).where(StewardSuggestion.owner_id == owner, StewardSuggestion.id == ident)
    row = await db.scalar(query.with_for_update() if lock else query)
    if not row:
        raise HTTPException(404, "找不到这条建议。")
    return row


async def decide(db, owner, ident, choice, option, revision):
    await begin_write(db)
    team = await teams.team_for(db, owner, lock=True)
    row = await owned(db, owner, ident, lock=True)
    state = {"approve":"approved", "defer":"deferred", "dismiss":"dismissed"}[choice]
    action = (option or "brief") if choice == "approve" else None
    if row.state == state and row.revision == revision + 1 and row.option == action:
        return public(row)
    can_reconsider = row.state == "deferred" and choice != "defer"
    if (row.state != "pending" and not can_reconsider) or row.revision != revision:
        raise HTTPException(409, "这条建议已处理或已经更新。")
    current = (await db.execute(_tasks(owner).where(WorkItem.id == row.task_id).with_for_update(of=WorkItem))).first()
    fresh = _observation(*current, datetime.now(UTC)) if current else None
    if not fresh or fresh["source_key"] != row.source_key or not team or not team.lead_agent_id:
        row.state = "expired"
        row.revision += 1
        await db.commit()
        raise HTTPException(409, "原任务或团队已变更，请查看最新建议。")
    item = current[0]
    if choice == "approve":
        rule = SIGNALS[row.kind]
        agent = await db.get(Agent, item.agent_id) if item.agent_id else None
        assignee = agent.name if agent and agent.owner_id == owner else "待指定"
        checklist = {
            "waiting":"请核对等待的前置资料是否已到，能否先完成独立部分。",
            "review":"请对照验收条件核对交付文件，并标记仍需复核的结果。",
            "overdue":"请核对已交付部分、剩余工作和新的可行日期。",
            "criteria":"请确定一个可检查的交付物、通过条件和边界情况。",
            "unassigned":"请根据岗位职责、可用资料和当前任务选择负责人。",
        }
        row.report = f"{rule.brief}\n{row.detail}\n当前负责人：{assignee}\n{checklist[row.kind]}"
        if action == "followup":
            ready = await db.scalar(select(WorkColumn.id).where(WorkColumn.board_id == item.board_id,
                                                                 WorkColumn.key == "ready"))
            if ready is None:
                raise HTTPException(409, "任务板目前没有可接单的栏目。")
            follow = WorkItem(owner_id=owner, board_id=item.board_id, column_id=ready,
                title=f"{rule.followup} · {item.title}", description=row.report,
                priority=item.priority, agent_id=team.lead_agent_id,
                position=await work_planner.next_position(db, item.board_id, ready))
            db.add(follow)
            await db.flush()
            work_events.add(db, follow, "created", actor="steward", detail=f"老板批准了原任务 #{item.id} 的跟进建议。")
            row.followup_task_id = follow.id
            row.report += f"\n已为管家建立跟进任务 #{follow.id}。"
    row.state, row.option, row.revision = state, action, revision + 1
    work_events.add(db, item, f"steward.{state}", key=f"steward:{row.id}:{state}", detail=row.report or row.title)
    await db.commit()
    return public(row)
