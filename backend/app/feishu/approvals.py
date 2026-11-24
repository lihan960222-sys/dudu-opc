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
        with db:
            if db.execute("select 1 from approval_dm where approval=?", (approval["id"],)).fetchone():
                continue
            detail = describe(approval)
            if detail is None:
                body = f"任务 #{task} 有一项操作需要在本机查看完整请求并决定。"
            else:
                body = f"任务 #{task} 请求一次操作许可。\n{detail}\n请引用本条私聊回复「确认」或「拒绝」。"
            outgoing = enqueue(db, task, "steward", {"text":body})
            db.execute("insert into approval_dm(approval,dispatch,thread,turn,outgoing,state) values(?,?,?,?,?,?)",
                       (approval["id"], execution["event"], execution["thread"], execution["turn"], outgoing,
                        "pending" if detail is not None else "local"))


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
