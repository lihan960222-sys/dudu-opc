"""Atomic channel/model binding. Only task identifiers and their assignees cross model boundaries."""
import hashlib
import json
from pathlib import Path

from sqlalchemy import select

from app.db.models import Agent,FeishuLeadChat,Thread,Turn,WorkItem
from app.db.models.feature import FeatureScope
from app.db.transactions import begin_write
from app.services import thread_store,role_workspace
from app.team.service import team_for


def signature(*fields):
    return hashlib.sha256(json.dumps(fields,ensure_ascii=False).encode()).hexdigest()


async def select_thread(cfg,*,with_model=False):
    from app.db.session import AsyncSessionLocal
    from app.services.thread_runtime import runtime
    anchor=cfg['steward_thread']
    owner=cfg['owner_id']
    identity=cfg['steward']
    project=cfg.get('project','')
    channel=signature(owner,identity['app_id'],identity['tenant_key'],identity['open_id'],project,anchor)
    async with AsyncSessionLocal() as db:
        await begin_write(db)
        team=await team_for(db,owner,lock=True)
        base=await db.get(Thread,anchor)
        lead=await db.get(Agent,identity['agent_id'])
        if not (team and lead and base and team.lead_agent_id==lead.id==base.agent_id
                and lead.owner_id==base.owner_id==owner and not lead.archived_at and base.kind=='chat'
                and not base.archived_at and not base.job_id and not base.room_id):
            raise ValueError('绑定的管家、老板或原会话已变更，请重新核对。')
        model=role_workspace.load(Path(lead.role_path), lead.model).model if lead.role_path else lead.model
        desired=signature(lead.provider,model)
        original=await db.get(FeishuLeadChat,anchor)
        if not original:
            # A previous reply may give the actual vendor even when this old thread
            # used an unpinned engine default. Never assign another vendor to it.
            last=await db.scalar(select(Turn.model).where(Turn.thread_id==anchor,Turn.model.is_not(None))
                                 .order_by(Turn.seq.desc()).limit(1))
            previous=last or base.model
            if not model and not base.model and not (last or '').startswith('deepseek'):
                previous=None
            original=FeishuLeadChat(thread_id=anchor,channel=channel,selection=signature(base.provider,previous),
                                    model=previous,project=project,isolated=False)
            db.add(original)
            await db.flush()
        elif original.channel!=channel:
            raise ValueError('此聊天已由另一飞书配置绑定。')
        binding=await db.scalar(select(FeishuLeadChat).where(FeishuLeadChat.channel==channel,FeishuLeadChat.selection==desired))
        if binding:
            existing=await db.get(Thread,binding.thread_id)
            if not existing or existing.owner_id!=owner or existing.agent_id!=lead.id or existing.archived_at or existing.kind!='chat':
                raise ValueError('对应模型的管家会话已归档或变更，请在本机核对绑定。')
            await db.commit()
            return (existing.id,binding.model) if with_model else existing.id
        adapter=runtime.adapters.get(lead.provider)
        if not adapter:
            raise ValueError('管家所选引擎不可用。')
        native=adapter.new_session_id()
        original.isolated=True
        thread=await thread_store.create_thread(db,owner_id=owner,agent_id=lead.id,provider=lead.provider,
            model=model,effort=lead.effort,workspace_path=base.workspace_path,provider_session_id=native,
            session_state='new' if native else 'none',title=f'{lead.name} · 飞书 · {model or lead.provider}',commit=False)
        db.add(FeishuLeadChat(thread_id=thread.id,channel=channel,selection=desired,model=model,project=project,isolated=True))
        await db.commit()
        return (thread.id,model) if with_model else thread.id


async def project_context(db,owner,project):
    rows=(await db.execute(select(WorkItem.id,WorkItem.agent_id).join(FeatureScope,FeatureScope.task_id==WorkItem.id)
         .where(WorkItem.owner_id==owner,FeatureScope.project==project).order_by(WorkItem.id.desc()).limit(24))).all()
    if not rows:
        return ''
    return '现有项目任务（任务编号、经办角色编号）：'+json.dumps([[a,b] for a,b in rows])


async def register_report(worker,row,turn):
    """Record only live Feishu arrivals whose native lead actually delegated."""
    from app.db import session
    from app.db.models import AgentJob
    from .routing import enqueue
    cfg=worker.cfg
    actor=cfg['steward'].get('agent_id')
    if not cfg.get('owner_id') or not actor:
        return False
    async with session.AsyncSessionLocal() as db:
        root=await db.scalar(select(AgentJob).where(AgentJob.owner_id==cfg['owner_id'],AgentJob.kind=='root',
            AgentJob.requester_agent_id==actor,AgentJob.assignee_agent_id==actor,AgentJob.requester_thread_id==row['thread'],
            AgentJob.requester_turn_id==row['turn']))
        if not root:
            return False
        names = list(await db.scalars(select(Agent.name).join(AgentJob,AgentJob.assignee_agent_id==Agent.id)
            .where(AgentJob.parent_id==root.id, AgentJob.owner_id==cfg['owner_id'], Agent.owner_id==cfg['owner_id'],
                   AgentJob.kind=='job').order_by(AgentJob.id)))
        if not names:
            return False
    key=row['app'],row['event']
    with worker.db:
        registered=worker.db.execute('insert or ignore into direct_team_reports(app,event,job) values(?,?,?)',(*key,root.id))
        if registered.rowcount and root.status not in ('completed','cancelled','failed'):
            # An acknowledgement is a receipt, not a request for owner input.
            assignees = '、'.join(dict.fromkeys(names))
            enqueue(worker.db,None,'steward',{'text':f'已交给 {assignees}，有结果我在这里回复。',
                                             'reply_to':row['message']})
        worker.db.execute("update inbox set state='done' where app=? and event=?",key)
    return True


async def publish_reports(worker):
    from app.db import session
    from app.db.models import AgentJob
    from .routing import enqueue
    cfg=worker.cfg
    actor=cfg.get('steward',{}).get('agent_id')
    if not actor or not cfg.get('owner_id'):
        return
    rows=worker.db.execute("""select r.job,r.question,i.*,d.thread,d.turn from direct_team_reports r
        join inbox i using(app,event) join direct_turns d using(app,event)
        where r.finished=0 and i.app=? order by r.checked,r.job limit 30""",(cfg['steward']['app_id'],)).fetchall()
    if not rows:
        return
    async with session.AsyncSessionLocal() as db:
        jobs={j.id:j for j in await db.scalars(select(AgentJob).where(AgentJob.id.in_([r['job'] for r in rows]),
            AgentJob.kind=='root',AgentJob.owner_id==cfg['owner_id'],AgentJob.requester_agent_id==actor,
            AgentJob.assignee_agent_id==actor))}
    for row in rows:
        job=jobs.get(row['job'])
        with worker.db:
            worker.db.execute('update direct_team_reports set checked=unixepoch() where job=?',(row['job'],))
            if not job or job.requester_thread_id!=row['thread'] or job.requester_turn_id!=row['turn']:
                worker.db.execute('update direct_team_reports set finished=1 where job=?',(row['job'],))
            elif job.status=='needs_input' and job.question and signature(job.question)!=row['question']:
                enqueue(worker.db,None,'steward',{'text':job.question[:2000],'reply_to':row['message']})
                worker.db.execute('update direct_team_reports set question=? where job=?',(signature(job.question),job.id))
            elif job.status in ('completed','failed','cancelled'):
                report=job.result if job.status=='completed' else job.outcome_detail
                if not report:
                    report='团队任务已结束。可在管家会话中查看派工记录。'
                worker.db.execute('update direct_team_reports set finished=1 where job=?',(job.id,))
                worker.direct_final(row,'done',report[:12000])
