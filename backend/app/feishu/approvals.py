import hashlib
import unicodedata
from pathlib import PureWindowsPath

from .routing import enqueue


def initialize(db):
    db.execute("""create table if not exists approval_dm(
        approval text primary key, dispatch text not null, thread integer not null, turn integer not null,
        outgoing text unique not null, decision_event text, state text not null default 'pending')""")

    existing = {row[1] for row in db.execute("pragma table_info(approval_dm)")}
    for column in ("decision","actor_open_id"):
        if column not in existing:
            db.execute(f"alter table approval_dm add column {column} text")


def describe(approval):
    if approval.get("tool") != "feature_files.write_text":
        return None
    args = approval.get("input") or {}
    name, text = args.get("path"), args.get("text")
    if not isinstance(name, str) or not isinstance(text, str) or not name or len(name) > 280:
        return None
    path = PureWindowsPath(name)
    if path.anchor or not path.parts or ".." in path.parts or any(unicodedata.category(c).startswith("C") for c in name):
        return None
    data = text.encode("utf-8")
    return f"写入 UTF-8 文件（已有文件将被替换）\n任务内路径：{name}\n大小：{len(data)} 字节\n内容摘要 SHA-256：{hashlib.sha256(data).hexdigest()}"


def notify(worker, execution, task):
    api, db = worker.api, worker.db
    result = api.get(f"threads/{execution['thread']}/approvals", params={"status":"pending"})
    result.raise_for_status()
    for approval in result.json():
        if approval["turn_id"] != execution["turn"] or approval["status"] != "pending":
            continue
        queue_notice(db, execution, task, approval, f"任务 #{task}")


def queue_notice(db, execution, task, approval, label):
    with db:
        if db.execute("select 1 from approval_dm where approval=?", (approval["id"],)).fetchone():
            return
        detail = describe(approval)
        if detail is None:
            body = f"{label} 有一项操作需要核对完整请求，请在管理网页的团队派工记录中处理。"
        else:
            body = f"{label} 请求一次操作许可。\n{detail}\n请引用本条私聊回复「确认」或「拒绝」。"
        outgoing = enqueue(db, task, "steward", {"text":body})
        db.execute("insert into approval_dm(approval,dispatch,thread,turn,outgoing,state) values(?,?,?,?,?,?)",
                   (approval["id"], execution["event"], execution["thread"], execution["turn"], outgoing,
                    "pending" if detail is not None else "local"))


async def collect_team_requests(worker):
    """Forward approvals from real employee dispatches to the owner's steward DM."""
    from datetime import UTC, datetime, timedelta
    from sqlalchemy import select
    from app.db import session
    from app.db.models import Agent, AgentJob, AgentJobEvent, Approval, Team, Thread, Turn
    from app.team.steward_review import preview
    owner = worker.cfg.get("owner_id")
    lead = worker.cfg.get("steward", {}).get("agent_id")
    if not owner or not lead:
        return
    query = (select(AgentJob, Approval, Agent)
             .select_from(Approval).join(Turn, Turn.id == Approval.turn_id)
             .join(Thread, Thread.id == Approval.thread_id)
             .join(AgentJob, AgentJob.assignee_turn_id == Turn.id)
             .join(Agent, Agent.id == AgentJob.assignee_agent_id).join(Team, Team.id == Agent.team_id)
             .where(Approval.status == "pending", Turn.status == "running", Turn.thread_id == Thread.id,
                    Team.owner_id == owner, Team.lead_agent_id == lead, Agent.owner_id == owner,
                    Thread.owner_id == owner, Thread.agent_id == Agent.id, Agent.archived_at.is_(None),
                    AgentJob.owner_id == owner, AgentJob.assignee_thread_id == Thread.id,
                    AgentJob.kind == "job", AgentJob.mode == "work"))
    async with session.AsyncSessionLocal() as db:
        for job, approval, agent in await db.execute(query):
            if (approval.created_at > datetime.now(UTC) - timedelta(minutes=3)
                    and await preview(db, owner, job, approval)):
                escalations = await db.scalars(select(AgentJobEvent.detail).where(
                    AgentJobEvent.job_id == job.id, AgentJobEvent.kind == "review_escalated"))
                if not any(e.get("approval_id") == approval.id for e in escalations):
                    continue
            item = {"id": approval.id, "tool": approval.tool, "input": approval.input}
            execution = {"thread": approval.thread_id, "turn": approval.turn_id, "event": f"team:{job.id}"}
            queue_notice(worker.db, execution, job.task_id, item, f"{agent.name} 的派工 #{job.id}")


def answer(worker, row, msg, text):
    db = worker.db
    if row["app"] != worker.cfg["steward"]["app_id"] or msg.get("chat_type") != "p2p":
        return False
    binding = db.execute("""select a.*, o.task from approval_dm a join outbox o on o.id=a.outgoing
        where o.remote=? and o.role='steward' and o.state='sent'""", (msg.get("parent_id"),)).fetchone()
    decision = {"确认":"allow", "拒绝":"deny"}.get(text.strip())
    if not binding:
        if decision:
            worker.respond(row,"done","请直接引用需要审批的管家私聊消息。")
        return bool(decision)
    task = binding["task"]
    if not decision or msg.get("message_type") != "text":
        worker.respond(row,"done","请直接引用原审批消息并回复「确认」或「拒绝」。",task)
        return True
    if binding["state"] != "pending":
        worker.respond(row,"done","该请求已处理、需在本机核对或已失效。",task)
        return True
    # Reserve the specific reply before posting. A crash after this point requires
    # reconciliation, never a second decision on a guessed or newer approval.
    with db:
        won = db.execute("""update approval_dm set state='verify',decision_event=?,decision=?,actor_open_id=?
            where approval=? and state='pending'""",(row["event"],decision,worker.cfg["steward"]["open_id"],binding["approval"])).rowcount
    if not won:
        worker.respond(row,"done","该请求已由另一条回复处理。",task)
        return True
    resp = worker.api.post(f"threads/{binding['thread']}/approvals/{binding['approval']}",
        json={"decision":decision, "remember":False, "expected_turn_id":binding["turn"]})
    if resp.status_code in (404,409):
        state, message = "expired", "请求已失效，或该轮任务已结束。"
    else:
        resp.raise_for_status()
        state, message = "answered", "已允许本次写入。" if decision == "allow" else "已拒绝本次写入。"
    with db:
        db.execute("update approval_dm set state=? where approval=?", (state,binding["approval"]))
        worker.respond(row,"done",message,task)
    return True
