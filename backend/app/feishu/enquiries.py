"""Answer only an owner's incoming DM to this app using scoped, published evidence."""
import json
import re
from pathlib import PurePath

from sqlalchemy import select
from .routing import enqueue
from .team_progress import STATUS


async def employee_updates(session, owner, employee, steward, text):
    """Show the verified owner their employee's dispatches across projects in DM."""
    from app.db.models import Agent, AgentJob, Team, Thread, Turn
    query = (select(AgentJob).join(Agent, Agent.id == AgentJob.assignee_agent_id)
             .join(Team, Team.id == Agent.team_id)
             .where(AgentJob.owner_id == owner, Agent.owner_id == owner, Team.owner_id == owner,
                    Team.lead_agent_id == steward, Agent.archived_at.is_(None),
                    Agent.id == employee, AgentJob.kind == "job", AgentJob.mode == "work"))
    number = re.search(r"派工\s*[#＃]?\s*(\d+)", text)
    if not number:
        rows = (await session.scalars(query.order_by(AgentJob.id.desc()).limit(10))).all()
        if not rows:
            return "目前没有派给我的任务。"
        lines = [f"派工 #{j.id} · {STATUS.get(j.status, j.status)} · {j.brief[:90]}" for j in rows]
        return "\n".join([*lines, "发送「派工 #编号」查看派工对话。"])
    job = await session.scalar(query.where(AgentJob.id == int(number[1])))
    if not job:
        return "找不到派给我的这份工作。"
    transcript = [f"派工 #{job.id} · {STATUS.get(job.status, job.status)}", "派工要求：" + job.brief]
    if job.assignee_thread_id and job.assignee_turn_id:
        reply = await session.scalar(select(Turn.result).join(Thread, Thread.id == Turn.thread_id).where(
            Thread.owner_id == owner, Thread.agent_id == employee, Thread.id == job.assignee_thread_id,
            Turn.id == job.assignee_turn_id))
        if reply:
            transcript.append("员工回复：" + reply)
    for label, value in (("员工提问：", job.question), ("收到的答复：", job.answer), ("汇报：", job.result)):
        if value and not any(value in line for line in transcript):
            transcript.append(label + value)
    record = "\n\n".join(transcript)
    page = re.search(r"第\s*(\d{1,6})\s*页", text)
    pages = (len(record) + 1999) // 2000
    index = min(max(int(page[1]) if page else 1, 1), pages)
    return record[(index-1)*2000:index*2000] + (
        f"\n\n第 {index}/{pages} 页。发送「派工 #{job.id} 第 {index+1} 页」继续。" if index < pages else "")


async def answer(worker,row,role,msg,text):
    from app.db import session
    from app.db.models import WorkItem, WorkColumn
    from app.db.models.feature import FeatureScope
    db = worker.db
    cfg = worker.cfg['roles'][role]
    verified = db.execute("""select message from owner_enquiries where app=? and event=? and owner_open_id=? and role=?""",
                          (row['app'],row['event'],cfg.get('owner_open_id'),role)).fetchone()
    group=db.execute('select * from group_enquiries where app=? and event=?',(row['app'],row['event'])).fetchone()
    valid_channel = msg.get('chat_type')=='p2p' and not group or (
        group and msg.get('chat_type')=='group' and group['chat']==msg.get('chat_id')==cfg['chat_id'])
    if not verified or verified['message']!=row['message'] or not valid_channel:
        with db:
            db.execute("update inbox set state='ignored' where app=? and event=?",(row['app'],row['event']))
        return
    if not group and re.search(r"派工|执行状态|聊天记录|工作进度|任务列表", text):
        async with session.AsyncSessionLocal() as owner:
            reply = await employee_updates(owner, worker.cfg['owner_id'], cfg['agent_id'],
                                           worker.cfg['steward'].get('agent_id'), text)
        with db:
            outgoing = enqueue(db, None, role, {'text': reply, 'enquiry_reply': True})
            db.execute('insert into enquiry_replies values(?,?,?,?)', (outgoing, row['app'],row['event'],role))
            db.execute("update inbox set state='done' where app=? and event=?", (row['app'],row['event']))
        return
    query = select(WorkItem.id,WorkItem.title,WorkColumn.name,FeatureScope.version,FeatureScope.acceptance).join(
        FeatureScope,FeatureScope.task_id==WorkItem.id).join(WorkColumn,WorkColumn.id==WorkItem.column_id).where(
        WorkColumn.board_id==WorkItem.board_id,WorkItem.owner_id==worker.cfg['owner_id'],WorkItem.agent_id==cfg['agent_id'],FeatureScope.project==worker.cfg['project'])
    explicit = re.search(r'[#＃](\d+)',text)
    ref = db.execute("""select o.task from enquiry_replies q join outbox o on o.id=q.outgoing
        where q.app=? and q.role=? and o.remote=? and o.state='sent'""",(row['app'],role,msg.get('parent_id'))).fetchone()
    task = int(explicit[1]) if explicit else ref['task'] if ref else None
    if group:
        task=group['task']
    async with session.AsyncSessionLocal() as owner:
        if task is not None:
            query = query.where(WorkItem.id==task)
        entries = (await owner.execute(query.order_by(WorkItem.updated_at.desc()).limit(50))).all()
    matches = [e for e in entries if e.title in text] if not task else entries
    if not matches and len(entries)==1:
        matches=entries
    selected = matches[0] if len(matches)==1 else None
    if selected:
        task = selected.id
        rows = db.execute("""select body,remote from outbox where role=? and task=? and state='sent'
            and id not in (select outgoing from enquiry_replies) order by rowid desc limit 5""",(role,task)).fetchall()
        evidence = []
        for item in rows:
            body=json.loads(item['body'])
            label = body.get('text') or PurePath(body.get('snapshot') or body.get('file','附件')).name
            evidence.append(label[:350])
        question=text
        text = f'〈{selected.title}〉 #{task}\n状态：{selected.name}'
        if group and explicit and int(explicit[1])!=task:
            text+='\n这里是本任务的话题。请到对应话题询问其他任务。'
        if selected.version and not group:
            text += '\n版本：'+selected.version[:200]
        if selected.acceptance and not group:
            text += '\n验收条件：'+selected.acceptance[:600]
        if group and any(word in question for word in ('验收','版本','依据','为什么','原因','证据')):
            relevant=[item for item in evidence if any(word in question and word in item
                      for word in ('验收','版本','依据','原因','证据'))]
            if not relevant:
                text+='\n群内现有记录还没有明确说明这一点。'
            else:
                evidence=relevant
        text += '\n最近的群内记录：\n'+'\n'.join(evidence) if evidence else '\n暂时没有已发布的工作记录。'
    elif group:
        task=group['task']
        text='此话题尚无可公开引用的本岗位任务记录；请让管家核对派工。'
    else:
        task = None
        text = ('请指定任务编号，例如「#12 现在进展如何？」\n' +
                '\n'.join(f'#{entry.id} · {entry.title}' for entry in (matches or entries)[:8])
                if entries else '发送「执行状态」查看管家派给我的工作，或把新任务交给管家。')
    with db:
        outgoing=enqueue(db,task,role,{'text':text,'enquiry_reply':True})
        db.execute('insert into enquiry_replies values(?,?,?,?)',(outgoing,row['app'],row['event'],role))
        db.execute("update inbox set state='done',task=? where app=? and event=?",(task,row['app'],row['event']))
