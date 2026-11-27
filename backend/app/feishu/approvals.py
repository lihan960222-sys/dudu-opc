import hashlib
import json
import re
import shlex
import sqlite3
import time
import unicodedata
from pathlib import PureWindowsPath
import httpx

from .routing import enqueue


def initialize(db):
    db.execute("""create table if not exists approval_dm(
        approval text primary key, dispatch text not null, thread integer not null, turn integer not null,
        outgoing text unique not null, decision_event text, state text not null default 'pending')""")

    existing = {row[1] for row in db.execute("pragma table_info(approval_dm)")}
    for column in ("decision","actor_open_id"):
        if column not in existing:
            db.execute(f"alter table approval_dm add column {column} text")
    for column in ("agent_id", "assignment_epoch"):
        if column not in existing:
            db.execute(f"alter table approval_dm add column {column} integer")
    if "offer_remember" not in existing:
        db.execute("alter table approval_dm add column offer_remember integer not null default 0")
    if "stale_review" not in existing:
        db.execute("alter table approval_dm add column stale_review integer not null default 0")
    if "offer_task" not in existing:
        db.execute("alter table approval_dm add column offer_task integer not null default 0")
    if "delivered_to" not in existing:
        db.execute("alter table approval_dm add column delivered_to text")
    if "delivered_at" not in existing:
        db.execute("alter table approval_dm add column delivered_at real")


def _exact_text(value, *, multiline=False, limit=1200):
    from app.services.decision_log import redact
    return (isinstance(value, str) and 0 < len(value) <= limit
            and not any(unicodedata.category(c).startswith("C") and not (multiline and c in "\r\n\t")
                        for c in value)
            and redact(value) == value)


def _public_parameters(value):
    if isinstance(value, dict):
        return all(isinstance(k,str) and not re.search(r"password|token|secret|api[_-]?key|credential|auth", k, re.I)
                   and _public_parameters(v) for k,v in value.items())
    if isinstance(value, list):
        return all(_public_parameters(v) for v in value)
    # Nested parameters are always serialized as JSON before being displayed.
    return not isinstance(value, str) or _exact_text(value, multiline=True)


def describe(approval, *, native=False):
    tool = approval.get("tool", "")
    args = approval.get("input") or {}
    if not isinstance(args, dict):
        return None
    if tool == "mcp__team__run_project_command" and set(args) == {"cwd", "argv"}:
        argv = args["argv"]
        if not (_exact_text(args["cwd"]) and isinstance(argv, list) and argv
                and all(_exact_text(arg, multiline=True, limit=6000) for arg in argv)):
            return None
        exact = json.dumps(args, ensure_ascii=False)
        if _exact_text(exact, limit=6000):
            return f"在员工项目中运行命令。完整参数：{exact}"
        return None
    if native and tool == "Bash":
        cmd, cwd = args.get("command"), args.get("cwd")
        extras = {k:v for k,v in args.items() if k not in ("command","cwd")}
        exact = json.dumps(extras, ensure_ascii=False, sort_keys=True)
        if _exact_text(cmd, multiline=True) and _exact_text(cwd) and _public_parameters(extras) and _exact_text(exact):
            if any(c in cmd for c in "\r\n\t"):
                # Show control characters as reversible JSON escapes, preserving literal backslashes.
                cmd = json.dumps(cmd, ensure_ascii=False)
                return f"执行命令（JSON 字符串）：{cmd}\n工作目录：{cwd}" + (f"\n请求参数：{exact}" if extras else "")
            return f"执行命令：{cmd}\n工作目录：{cwd}" + (f"\n请求参数：{exact}" if extras else "")
        return None
    if native and tool == "mcp__team__deliver_project_file":
        exact = json.dumps(args, ensure_ascii=False, sort_keys=True)
        if _public_parameters(args) and _exact_text(exact):
            replacing = "更新指定版本的文件" if args.get("previous_sha256") else "另存工单产物，保留已有文件"
            return f"{replacing}\n来源：{args.get('source')}\n目标目录：{args.get('folder')}\n完整参数：{exact}"
        return None
    if native and tool in ("apply_patch", "codex.request_permissions"):
        exact = json.dumps(args, ensure_ascii=False, sort_keys=True)
        if args and _public_parameters(args) and _exact_text(exact):
            return f"操作：{tool}\n完整参数：{exact}"
        return None
    if native and isinstance(tool, str) and tool.startswith("mcp__community__"):
        exact = json.dumps(args, ensure_ascii=False, sort_keys=True)
        sensitive = re.search(r'"[^"]*(?:password|token|secret|api[_-]?key|credential|auth)[^"]*"\s*:', exact, re.I)
        if not sensitive and _exact_text(exact) and re.fullmatch(r"mcp__community__[A-Za-z0-9_-]{1,128}", tool):
            return f"调用工具：{tool}\n完整参数：{exact}"
        return None
    if tool != "feature_files.write_text" or set(args) != {"path", "text"}:
        return None
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
    execution = dict(execution)  # executions comes from the gateway's SQLite journal
    result = api.get(f"threads/{execution['thread']}/approvals", params={"status":"pending"})
    result.raise_for_status()
    for approval in result.json():
        if approval["turn_id"] != execution["turn"] or approval["status"] != "pending":
            continue
        if approval.get("employee_rememberable") is not None and execution.get("assignment_epoch") is None:
            # The native dispatch collector verifies the employee and current assignment.
            # A legacy inbox execution cannot authorize or bind a native project request.
            continue
        queue_notice(db, execution, task, approval, f"任务 #{task}")


_LOG_QUERY = re.compile(
    r"Get-ChildItem -LiteralPath (?P<paths>'[^'\r\n]+'(?:,\s*'[^'\r\n]+'){0,9})"
    r" -Recurse -Filter '(?P<pattern>[\w.*?-]+)'(?: -ErrorAction SilentlyContinue)?"
    r"\s*\|\s*Sort-Object LastWriteTime -Descending\s*\|\s*Select-Object -First (?P<count>\d{1,3}) "
    r"FullName,LastWriteTime(?:\s*\|\s*Format-List)?", re.I)


def _card_display(approval, label, detail):
    """Called only after describe() has approved the fields for this channel."""
    tool, args = approval["tool"], approval["input"]
    if tool == "mcp__team__run_project_command":
        summary = f"在员工项目目录 {args['cwd']} 运行 {PureWindowsPath(args['argv'][0]).name}，请展开核对具体操作。"
    elif tool == "Bash":
        cmd, argv = args["command"], args.get("argv")
        if (isinstance(argv, list) and len(argv) == 4 and all(isinstance(a, str) for a in argv)
                and shlex.join(argv) == cmd and PureWindowsPath(argv[0]).name.lower() in ("pwsh.exe", "powershell.exe")
                and [a.lower() for a in argv[1:3]] == ["-noprofile", "-command"]):
            cmd = argv[-1]
        elif argv is None:
            wrapper = re.fullmatch(r"""["']([^"'\r\n]+)["'] -NoProfile -Command (["'])(.+)\2""", cmd, re.I | re.S)
            if wrapper and PureWindowsPath(wrapper[1]).name.lower() in ("pwsh.exe", "powershell.exe"):
                cmd = wrapper[3]
        from app.services.employee_approval_rules import _PROCESS_QUERY, _LAUNCH, directory_inspection
        query = _LOG_QUERY.fullmatch(cmd.strip())
        if query:
            action = f"在指定目录及子目录查找 {query['pattern']}，按修改时间列出最近 {query['count']} 个文件"
        elif directory_inspection(cmd):
            action = "查看工作目录中的文件或当前位置"
        elif _PROCESS_QUERY.fullmatch(cmd):
            action = "查询指定进程及其状态"
        elif launch := _LAUNCH.fullmatch(cmd):
            action = f"启动程序 {PureWindowsPath(launch[1]).name}"
        else:
            action = "申请运行终端命令（预览）"
        first_line = cmd.splitlines()[0]
        snippet = first_line[:100]
        if len(snippet) < len(cmd):
            snippet += "…"
        summary = f"{action}\n{snippet}\n工作目录：{args['cwd']}"
        if isinstance(args.get("reason"), str) and args["reason"].strip():
            reason = " ".join(args["reason"].split())
            summary += "\n员工说明：" + (reason if len(reason) <= 100 else reason[:100] + "…")
        if args.get("additionalPermissions") or args.get("networkApprovalContext"):
            summary += "\n这条命令还申请了额外权限，请展开查看范围。"
    elif tool == "mcp__team__deliver_project_file":
        summary = ("更新指定版本的文件" if args.get("previous_sha256") else "把项目产物另存到您指定的文件夹")
        summary += f"\n来源：{args.get('source')}\n目标目录：{args.get('folder')}"
    elif tool == "feature_files.write_text":
        return {"title":label,"summary":f"写入 {args['path']}，已有文件将被替换。","details":detail}
    else:
        summary = {"apply_patch":"申请修改项目文件", "codex.request_permissions":"申请额外权限"}.get(tool, f"申请调用工具 {tool}")
    return {"title":label, "summary":summary, "details":json.dumps(args, ensure_ascii=False, indent=2)}


def queue_notice(db, execution, task, approval, label):
    with db:
        previous = db.execute("select * from approval_dm where approval=?", (approval["id"],)).fetchone()
        epoch, agent_id = execution.get("assignment_epoch"), execution.get("agent_id")
        if previous:
            if (previous["state"] not in ("local","expired","unconfirmed") or previous["thread"] != execution["thread"]
                    or previous["turn"] != execution["turn"]):
                return
            same_assignment = (previous["dispatch"] == execution["event"] and
                               (previous["assignment_epoch"], previous["agent_id"]) == (epoch, agent_id))
            legacy = previous["state"] == "local" and previous["assignment_epoch"] is None and previous["agent_id"] is None
            if not same_assignment and not (legacy and epoch is not None and agent_id is not None):
                return
        detail = describe(approval, native=epoch is not None and agent_id is not None)
        if previous and not detail and not approval.get("retired"):
            return
        offer = bool(detail and approval.get("can_remember") and epoch is not None and agent_id is not None)
        offer_task = offer and bool(task) and bool(approval.get("can_scope_task"))
        deny_only = detail is None and execution.get("recovery_reply")
        if deny_only:
            body = f"老板，{label} 有一项操作的完整参数不适合发到飞书。我已保留原申请。您可以引用这条消息回复「拒绝」，让员工停止这项操作。"
        elif detail is None:
            body = f"老板，{label} 有一项操作需要您决定。完整请求在管理网页的团队派工记录里，您可以在那里核对和处理。"
        else:
            body = f"老板，{label} 有一项操作需要您的许可。\n{detail}\n您可以引用这条私聊，回复「确认」或「拒绝」。"
        if offer:
            body = (f"老板，{label} 需要您决定。\n{detail}\n"
                    "引用回复「允许」，以后同员工、同项目、同一操作由大嘴代批，可在工作台撤销。"
                    "也可以回复「仅本次」或「拒绝」。")
        if offer_task:
            body = (f"老板，{label} 需要您决定。\n{detail}\n引用回复「允许」后，大嘴可在本工单代批同一员工的相同操作。"
                    "也可以选「仅本次」「允许并记住」（同项目持续有效）或「拒绝」。")
        if offer and approval["tool"] == "mcp__team__deliver_project_file":
            suffix = PureWindowsPath(approval["input"]["source"]).suffix.lower()
            body += f"\n代批范围包括员工从当前工单目录向这个文件夹另存其他 {suffix} 产物。已有文件会保留。"
        retired = bool(approval.get("retired"))
        if retired:
            offer = offer_task = False
            body = (f"老板，{label} 上一项操作的申请已经失效。\n{detail or '完整内容保存在工作台。'}"
                    "\n点「只读核对」可让员工在原工单查已有结果。")
        stale_review = bool((detail or retired) and approval.get("can_scope_task") and task and epoch is not None)
        if stale_review:
            body += "\n原申请过期后再允许，员工只会核对已有结果。"
        notice = {"text":body}
        from app.services.steward_reply import LIMIT
        if detail is not None or retired:
            # The limit applies to ordinary steward replies. Cards carry the full
            # reviewed request in their own details, including long workstation paths.
            if len(body) > LIMIT:
                notice["text"] = f"老板，{label} 的审批卡已列出完整参数。请核对卡片后选择。"
            notice["approval_card"] = approval["id"]
            notice["approval_display"] = (_card_display(approval, label, detail) if detail else
                                          {"title":label, "summary":"", "details":"完整请求可在工作台核对。"})
            if offer and approval["tool"] == "mcp__team__deliver_project_file":
                notice["approval_display"]["summary"] += f"\n代批范围包括同一工单目录、同一目标文件夹中的其他 {suffix} 产物。"
            if retired:
                notice["approval_display"]["summary"] = "原申请已失效。只读核对只会检查现有结果。"
                notice["review_only"] = True
            elif stale_review:
                notice["approval_display"]["summary"] += "\n过期后再允许，员工只会核对已有结果。"
        if execution.get("source_message"):
            notice["reply_to"] = execution["source_message"]
        if previous:
            # Only a live backend request reaches this routine. Replace a previous
            # local-only notice exactly once; a reply to its old remote is invalid.
            changed = db.execute("""delete from approval_dm where approval=? and outgoing=? and state=?""",
                                 (approval["id"], previous["outgoing"], previous["state"])).rowcount
            if not changed:
                return
            db.execute("update outbox set state='superseded' where id=? and state='pending'", (previous["outgoing"],))
        outgoing = enqueue(db, task, "steward", notice)
        db.execute("insert into approval_dm(approval,dispatch,thread,turn,outgoing,agent_id,assignment_epoch,offer_remember,offer_task,stale_review,state) values(?,?,?,?,?,?,?,?,?,?,?)",
                   (approval["id"], execution["event"], execution["thread"], execution["turn"], outgoing, agent_id, epoch, int(offer), int(offer_task), int(stale_review),
                    "pending" if detail is not None or retired else "deny_only" if deny_only else "local"))


async def collect_team_requests(worker):
    """Forward approvals from real employee dispatches to the owner's steward DM."""
    from datetime import UTC, datetime, timedelta
    from sqlalchemy import and_, or_, select
    from app.db import session
    from app.db.models import Agent, AgentJob, AgentJobEvent, Approval, Team, Thread, Turn
    from app.services.feature_tasks import assignment_current
    from app.team.steward_review import effective_job_mode, preview
    owner = worker.cfg.get("owner_id")
    lead = worker.cfg.get("steward", {}).get("agent_id")
    if not owner or not lead:
        return
    query = (select(AgentJob, Approval, Agent)
             .select_from(Approval).join(Turn, Turn.id == Approval.turn_id)
             .join(Thread, Thread.id == Approval.thread_id)
             .join(AgentJob, AgentJob.assignee_turn_id == Turn.id)
             .join(Agent, Agent.id == AgentJob.assignee_agent_id).join(Team, Team.id == Agent.team_id)
             .where(or_(and_(Approval.status == "pending", Turn.status == "running"),
                        and_(Approval.status.in_(("expired", "cancelled")), AgentJob.outcome == "awaiting_native_review",
                             AgentJob.current_turn_id.is_(None), AgentJob.status == "needs_input",
                             select(AgentJobEvent.id).where(AgentJobEvent.job_id == AgentJob.id,
                                AgentJobEvent.kind == "native_request_retired",
                                AgentJobEvent.detail["approval_id"].as_string() == Approval.id).exists())),
                    Turn.thread_id == Thread.id,
                    Team.owner_id == owner, Team.lead_agent_id == lead, Agent.owner_id == owner,
                    Thread.owner_id == owner, Thread.agent_id == Agent.id, Agent.archived_at.is_(None),
                    AgentJob.owner_id == owner, AgentJob.assignee_thread_id == Thread.id,
                    AgentJob.kind == "job", AgentJob.mode == "work"))
    async with session.AsyncSessionLocal() as db:
        for job, approval, agent in await db.execute(query):
            sent = worker.db.execute("select state from approval_dm where approval=?", (approval.id,)).fetchone()
            if sent and sent[0] in ("pending", "answered", "verify", "deny_only"):
                continue
            if not await assignment_current(db, job):
                continue
            if job.requester_agent_id != lead or agent.manager_agent_id not in (None, lead):
                if not job.task_id or await effective_job_mode(db, job) == "plan":
                    continue
            if (approval.created_at > datetime.now(UTC) - timedelta(minutes=3)
                    and await preview(db, owner, job, approval)):
                escalations = await db.scalars(select(AgentJobEvent.detail).where(
                    AgentJobEvent.job_id == job.id, AgentJobEvent.kind == "review_escalated"))
                if not any(e.get("approval_id") == approval.id for e in escalations):
                    continue
            item = {"id": approval.id, "tool": approval.tool, "input": approval.input,
                    "retired": approval.status != "pending"}
            execution = {"thread": approval.thread_id, "turn": approval.turn_id, "event": f"team:{job.id}"}
            thread = await db.get(Thread, approval.thread_id)
            if job.task_id and (thread.provider_state or {}).get("employee_project"):
                execution.update(assignment_epoch=job.assignment_epoch, agent_id=agent.id)
                item["can_scope_task"] = True
                from app.services.employee_approval_rules import identity
                from app.services.feature_tasks import project_runtime_for
                stamp = thread.provider_state["employee_project"]
                if isinstance(stamp, dict) and stamp.get("root"):
                    import asyncio
                    try:
                        project = await project_runtime_for(db, owner, agent, thread, job.approval_mode)
                    except (ValueError, OSError):
                        project = None
                    item["can_remember"] = bool(await asyncio.to_thread(identity, project, approval.tool, approval.input))
            if worker.db.execute("select 1 from sqlite_master where name='direct_turns'").fetchone():
                original = await db.get(AgentJob, job.root_id) if job.root_id else job
                from app.feishu import dispatch_recovery
                if dispatch_recovery.is_recovery(original):
                    target=dispatch_recovery.original_reply(worker.db,worker.cfg,original)
                    if not target:
                        continue
                    execution.update(source_message=target,recovery_reply=True)
                elif original and original.owner_id == owner and original.requester_agent_id == lead:
                    origin = worker.db.execute("""select i.message from inbox i join direct_turns d using(app,event)
                        where i.app=? and d.thread=? and d.turn=?""",
                        (worker.cfg["steward"].get("app_id"), original.requester_thread_id, original.requester_turn_id)).fetchone()
                    if origin and origin[0]:
                        execution["source_message"] = origin[0]
            reference = f"工单 #{job.task_id}" if job.task_id else f"派工 #{job.id}"
            queue_notice(worker.db, execution, job.task_id, item, f"{agent.name} · {reference}")


def delivered(db, outgoing, steward):
    identity = [steward.get(k) for k in ("app_id", "tenant_key", "open_id")]
    if all(identity):
        db.execute("update approval_dm set delivered_to=?, delivered_at=? where outgoing=?",
                   (json.dumps(identity), time.time(), outgoing))


async def _live_owner_requests(owner):
    from sqlalchemy import select
    from app.db import session
    from app.db.models import Approval, Thread, Turn
    async with session.AsyncSessionLocal() as db:
        result = await db.execute(select(Approval.id, Approval.thread_id, Approval.turn_id)
            .join(Thread, Approval.thread_id == Thread.id).join(Turn, Approval.turn_id == Turn.id)
            .where(Thread.owner_id == owner, Turn.thread_id == Thread.id, Turn.status == "running",
                   Approval.status == "pending"))
        return result.all()


async def owner_request(worker, row, msg, text):
    """Resolve an unquoted one-time choice only against a unique delivered, live request."""
    choice = text.strip().rstrip("。！？!?").strip()
    inquire = re.fullmatch(r"(?:大嘴[，, ]*)?(?:请|麻烦)?(?:你|您)?(?:帮我|替我|帮忙|代我)?"
                           r"(?:审批|审核|处理审批)(?:一下|下)?[吧呢啊呀嘛啦呗]*", choice)
    explicit = choice in ("允许", "仅本次", "拒绝")
    if not (inquire or explicit) or msg.get("chat_type") != "p2p" or msg.get("message_type") != "text":
        return False
    if explicit and (msg.get("parent_id") or msg.get("root_id")):
        return False  # An existing quote always identifies its own card.
    steward = worker.cfg["steward"]
    identity = [steward.get(k) for k in ("app_id","tenant_key","open_id")]
    if (not all(identity) or row["app"] != identity[0] or not worker.cfg.get("owner_id") or
            not worker.db.execute("""select 1 from owner_dm_sources where app=? and event=? and message=?
                and tenant=? and owner=? and chat=?""", (row["app"], row["event"], msg.get("message_id"),
                identity[1], identity[2], msg.get("chat_id"))).fetchone()):
        worker.respond(row, "done", "这条消息没有绑定到老板的私聊。")
        return True
    live = await _live_owner_requests(worker.cfg["owner_id"])
    if not live:
        worker.respond(row, "done", "老板，目前后台没有待审批的操作。")
        return True
    selected = []
    for request, thread, turn in live:
        binding = worker.db.execute("""select a.*,o.remote,o.body,o.task from approval_dm a join outbox o on o.id=a.outgoing
            where a.approval=? and a.thread=? and a.turn=? and o.role='steward' and o.state='sent' and o.remote is not null
            and a.state in ('pending','deny_only')""", (request,thread,turn)).fetchone()
        if binding:
            selected.append(binding)
    if len(live) == len(selected) == 1:
        binding = selected[0]
        body = json.loads(binding["body"])
        try:
            issued = int(msg.get("create_time") or 0) / 1000
        except (ValueError, TypeError):
            issued = 0
        sent = binding["delivered_at"] or 0
        if (explicit and binding["state"] == "pending" and body.get("approval_card") == binding["approval"]
                and binding["delivered_to"] == json.dumps(identity) and sent <= issued <= time.time()
                and 0 <= time.time() - sent <= 600):
            return answer(worker, row, {**msg, "parent_id":binding["remote"]},
                          "仅本次" if choice == "允许" and not binding["offer_task"] else choice)
    lines = [f"老板，后台有 {len(live)} 项操作正等审批。"]
    for binding in selected[:5]:
        notice = json.loads(binding["body"])
        display = notice.get("approval_display") or {}
        if display.get("summary"):
            lines.append(f"{display['title']}：{display['summary']}")
        elif notice.get("text"):
            # A delivered pre-upgrade notice already contains a reviewed action description.
            lines.append(notice["text"].partition("\n引用回复")[0].partition("\n您可以引用")[0][:240])
    lines.append("这些申请还在等决定。请在对应卡片选「仅本次」或「拒绝」。"
                 if selected else "待批操作还没有可对应的飞书卡片，可以在工作台的审批记录里核对。")
    if len(selected) == 1 and selected[0]["offer_remember"]:
        lines.append("这张卡还可以选「允许并记住」，授权大嘴以后代批同一员工、同一项目、同一操作。")
    worker.respond(row, "done", "\n".join(lines))
    return True


def answer(worker, row, msg, text):
    db = worker.db
    if row["app"] != worker.cfg["steward"]["app_id"] or msg.get("chat_type") != "p2p":
        return False
    binding = db.execute("""select a.*, o.task from approval_dm a join outbox o on o.id=a.outgoing
        where o.remote=? and o.role='steward' and o.state='sent'""", (msg.get("parent_id"),)).fetchone()
    decision = {"确认":"allow", "允许":"allow", "允许并记住":"allow", "仅本次":"allow", "拒绝":"deny"}.get(text.strip())
    if binding and text.strip() == "只读核对" and binding["stale_review"]:
        decision = "allow"
    if binding and text.strip() == "本工单允许" and binding["offer_task"]:
        decision = "allow"
    if not binding:
        if decision:
            worker.respond(row,"done","老板，我还没对上您要处理的申请。您方便引用原来的审批消息吗？")
        return bool(decision)
    task = binding["task"]
    if binding["state"] == "deny_only" and (decision != "deny" or msg.get("message_type") != "text"):
        worker.respond(row,"done","老板，这项操作的完整参数无法在飞书核对。您可以引用原审批消息回复「拒绝」，让员工停止这项操作。",task)
        return True
    if not decision or msg.get("message_type") != "text":
        worker.respond(row,"done","老板，我还不能确定您的选择。您可以引用原审批消息，回复「确认」或「拒绝」。",task)
        return True
    if binding["state"] not in ("pending","deny_only"):
        worker.respond(row,"done","老板，我没能再处理这份申请。它可能已经处理或失效，也可能需要在本机核对。",task)
        return True
    # Reserve the specific reply before posting. A crash after this point requires
    # reconciliation, never a second decision on a guessed or newer approval.
    with db:
        won = db.execute("""update approval_dm set state='verify',decision_event=?,decision=?,actor_open_id=?
            where approval=? and state=?""",(row["event"],decision,worker.cfg["steward"]["open_id"],binding["approval"],binding["state"])).rowcount
    if not won:
        worker.respond(row,"done","老板，我查到这份申请已有另一条回复在处理。",task)
        return True
    remember = bool(binding["offer_remember"] and text.strip() in ("允许","允许并记住","本工单允许"))
    payload = {"decision":decision, "remember":remember, "expected_turn_id":binding["turn"]}
    if decision == "allow" and binding["stale_review"]:
        payload["review_if_stale"] = True
    if remember and binding["offer_task"] and text.strip() in ("允许", "本工单允许"):
        payload["task_scope"] = True
    if binding["assignment_epoch"] is not None:
        payload.update(expected_assignment_epoch=binding["assignment_epoch"], expected_employee_id=binding["agent_id"])
    try:
        resp = worker.api.post(f"threads/{binding['thread']}/approvals/{binding['approval']}", json=payload)
    except httpx.RequestError:
        resp = None
    if resp is None or not resp.is_success:
        state, message = _readback_decision(worker, binding)
    else:
        resp.raise_for_status()
        state, message = "answered", "老板，我已按您的选择允许本次操作。" if decision == "allow" else "老板，我已按您的选择拒绝本次操作。"
        if payload.get("review_if_stale") and resp.json().get("status") == "review_requested":
            message = "老板，原申请已失效。已登记在原工单只读核对已有结果，旧命令不会重发。"
        elif remember:
            data = resp.json()
            message = ("老板，已记录您的选择。本次决定送达后，大嘴会按所选范围代批。" if data.get("rule_id") else
                       "老板，这次已允许。"+(data.get("remember_error") or "长期授权没有保存。"))
    with db:
        db.execute("update approval_dm set state=? where approval=?", (state,binding["approval"]))
        worker.respond(row,"done",message,task)
    return True


def _readback_decision(worker, binding):
    """A refused/ambiguous POST does not tell us whether Codex is still waiting."""
    try:
        result = worker.api.get(f"threads/{binding['thread']}/approvals")
        result.raise_for_status()
        status = next((a["status"] for a in result.json() if a["id"] == binding["approval"]
                       and a["turn_id"] == binding["turn"]), None)
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        status = None
    if status == "pending":
        return binding["state"], "老板，后台显示原申请仍在等待。本次答复尚未生效；可以在工作台核对，或稍后再回复原审批消息。"
    if status in ("allowed","denied"):
        decision = "允许" if status == "allowed" else "拒绝"
        return "answered", f"老板，后台记录的决定是「{decision}」。员工的执行结果以任务回执为准。"
    if status in ("expired","cancelled"):
        return "expired", "老板，已核实原申请已过期或已取消，这次回复无法再放行它。"
    return "unconfirmed", "老板，暂时查不到这条申请的最新状态。我会继续核对待批记录。"


def confirmed_owner_grant(owner, approval, thread, turn, employee, epoch, *, task_scope=False):
    """A manage/chat device alone cannot create a standing rule. Verify the owner's bound DM."""
    from .dispatch_recovery import channel
    from .workspace_access import config_from_env
    try:
        cfg = config_from_env()
        if cfg["owner_id"] != owner:
            return False
        who = cfg["steward"]
        with channel(cfg) as db:
            row = db.execute("""select i.payload,o.remote,a.offer_task,a.delivered_to,a.delivered_at,s.chat from approval_dm a join outbox o on a.outgoing=o.id
                join owner_dm_sources s on s.app=? and s.event=a.decision_event and s.tenant=? and s.owner=?
                join inbox i using(app,event,message)
                where a.approval=? and a.thread=? and a.turn=? and a.agent_id=? and a.assignment_epoch=?
                and a.offer_remember=1 and a.state='verify' and a.decision='allow' and a.actor_open_id=s.owner
                and o.role='steward' and o.state='sent'""",
                (who["app_id"],who["tenant_key"],who["open_id"],approval,thread,turn,employee,epoch)).fetchone()
        if not row:
            return False
        msg = json.loads(row["payload"])
        choices = (("允许", "本工单允许") if task_scope and row["offer_task"] else
                   ("允许并记住",) if row["offer_task"] else ("允许", "允许并记住"))
        if msg.get("chat_type") != "p2p" or msg.get("message_type") != "text":
            return False
        choice = json.loads(msg["content"])["text"].strip()
        if msg.get("parent_id") == row["remote"]:
            return choice in choices
        # owner_request bound this unquoted reply to the sole live, delivered
        # card. A device cannot turn this shortcut into a standing project grant.
        if (not task_scope or not row["offer_task"] or choice != "允许" or msg.get("parent_id")
                or msg.get("root_id") or msg.get("chat_id") != row["chat"] or not row["delivered_at"]):
            return False
        sent = row["delivered_at"]
        issued = int(msg.get("create_time") or 0) / 1000
        return (row["delivered_to"] == json.dumps([who["app_id"], who["tenant_key"], who["open_id"]])
                and sent <= issued <= time.time() and 0 <= time.time() - sent <= 600)
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error):
        return False
