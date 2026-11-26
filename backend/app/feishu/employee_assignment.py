"""Bind an authenticated owner's requested action to the real lead/employee job."""
import json

from . import document_access, documents, history, submissions, workspace_access


async def route(worker, row, msg, text):
    review = history.REVIEW.fullmatch(history.canonical(text))
    linked = document_access.request(text)
    document = documents.is_command(text)
    if not linked and not document and not (review and review[1]):
        return False
    cfg, db = worker.cfg, worker.db
    who = cfg["steward"]
    keys = row["app"],row["event"]
    proof = db.execute("""select 1 from owner_readwrite_requests where app=? and event=?
        and message=? and owner=? and tenant=?""",(*keys,row["message"],who.get("open_id"),who.get("tenant_key"))).fetchone()
    if not (proof and who["app_id"]==row["app"] and msg.get("chat_type")=="p2p"
            and msg.get("message_type")=="text" and msg.get("message_id")==row["message"]):
        with db:
            db.execute("update inbox set state='ignored' where app=? and event=? and state='received'",keys)
        return True
    current = db.execute("select state from inbox where app=? and event=?",keys).fetchone()
    if current[0]!="received":
        return True
    if not cfg.get("steward_thread"):
        worker.direct_final(row,"done","请先在工作台绑定大嘴的飞书会话，再派这项任务。")
        return True
    raw = json.loads(msg["content"])["text"]
    requirement = raw
    contact = title = body = ""
    try:
        if linked:
            employee_name,action,title,body=linked
            _,employee,_=await documents.writer(cfg,employee_name)
            if action=="edit_document" and body:
                documents.blocks(body)
                # Exact replacements and explicit append drafts are data; other
                # owner prose stays in the brief for the employee to interpret.
                first=raw.partition("\n")[0].replace(title,"")
                if not document_access.replacement(body) and not any(v in first for v in ("追加","补写")):
                    body=""
        elif document:
            named = documents.NAMED.match(text.strip())
            employee_name = named[1] if named else "二狗"
            _, employee, _ = await documents.writer(cfg,employee_name)
            action = "create_document"
            first, _, draft = documents.NAMED.sub("",text.strip(),count=1).partition("\n")
            title = documents.COMMAND.sub("",first,count=1).lstrip("：: ").strip()
            body = draft if draft.strip() else documents.quoted_answer(db,row,msg)
            if not title or len(title)>200:
                raise ValueError("请告诉我文档标题。")
            if body.strip():
                documents.blocks(body)
        else:
            employee_name,contact = review[1],review[3]
            source = workspace_access.settings_for(cfg)["history_sources"].get(contact)
            if not source:
                raise ValueError(f"请先为{employee_name}绑定与{contact}的单聊。")
            employee, *_ = await history.reviewer(cfg,source,employee_name)
            action = {"全部并重整知识库":"restructure","全部并写入云盘":"update"}.get(review[5].strip(),"read")
            decision=db.execute("select decision from knowledge_intent_candidates where app=? and event=?",keys).fetchone()
            from .ingress import FULL_PUBLICATION
            if (history.SHORT_RETRY.fullmatch(raw.strip()) or FULL_PUBLICATION.fullmatch(raw.strip())
                    or decision and decision[0]=="resume"):
                prior=db.execute("""select a.original from knowledge_followups f
                    join employee_feishu_assignments a on a.app=f.app and a.event=f.parent_event
                    where f.app=? and f.message=? and a.employee=? and a.contact=? and a.action=?""",
                    (row["app"],row["message"],employee,contact,action)).fetchone()
                if prior:
                    requirement=prior[0]
        with db:
            db.execute("""insert or ignore into employee_feishu_assignments
                (app,event,message,employee,command,original,event_key,contact,action,title,body)
                values(?,?,?,?,?,?,?,?,?,?,?)""",(*keys,row["message"],employee,text,requirement,submissions.event_key(row),
                         contact,action,title,body))
    except (ValueError,KeyError,OSError) as err:
        worker.direct_final(row,"done",str(err))
        return True
    goal = text.partition("\n")[0]
    detail = raw.partition("\n")[0] if document else raw
    prompt = (f"老板来信：{detail}\n已核对的飞书任务：{goal}\n"
              f"请用 delegate 把这次任务交给{employee_name}（员工编号 {employee}），由员工执行自己的飞书工具后向你汇报。"
              "请保留老板提出的范围、结构和交付要求，再向老板汇总实际结果。")
    await worker.route_direct(row,msg,prompt)
    return True


async def latest_status(worker, app, contact):
    """Read the employee's actual child job for the latest verified assignment."""
    if not worker.db.execute("select 1 from sqlite_master where name='direct_turns' and type='table'").fetchone():
        return None
    assignment = worker.db.execute("""select a.employee,d.thread,d.turn from employee_feishu_assignments a
        left join direct_turns d using(app,event) where a.app=? and a.contact=?
        order by a.rowid desc limit 1""",(app,contact)).fetchone()
    if not assignment:
        return None
    if not assignment["turn"]:
        return "大嘴已收到，正在准备派工。"
    from sqlalchemy import select
    from app.db.models import AgentJob
    from app.db.session import AsyncSessionLocal
    owner = worker.cfg["owner_id"]
    async with AsyncSessionLocal() as session:
        root = await session.scalar(select(AgentJob.id).where(AgentJob.owner_id==owner,
            AgentJob.requester_thread_id==assignment["thread"],AgentJob.requester_turn_id==assignment["turn"],
            AgentJob.kind=="root"))
        child = await session.scalar(select(AgentJob).where(AgentJob.owner_id==owner,AgentJob.parent_id==root,
            AgentJob.assignee_agent_id==assignment["employee"],AgentJob.kind=="job").order_by(AgentJob.id.desc()).limit(1)) if root else None
    if not child:
        return "已交给大嘴，目前还没有员工派工回执。"
    label = {"completed":"已完成","failed":"执行失败","cancelled":"已取消","needs_input":"等待补充信息",
             "queued":"等待执行","running":"正在执行","waiting":"正在等待结果"}.get(child.status,"处理中")
    return f"派工 #{child.id}，{label}。"
