"""Mirror verified scoped work assignments. Private consultations stay in the team ledger."""
import logging
import time

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from .routing import bind_outgoing, enqueue, ensure_group_root

STATUS = {
    'queued':'已排队','admitted':'已领取','dispatched':'已派单','running':'执行中',
    'waiting':'等待处理','completed':'已完成','cancelled':'已取消','failed':'执行失败',
    'needs_input':'需要补充信息','blocked':'受阻',
}
TERMINAL = {'completed','cancelled','failed'}


def initialize(db):
    db.executescript("""
    create table if not exists team_cursor(singleton integer primary key, last_job integer not null);
    create table if not exists team_publications(job integer primary key,task integer,role text,status text, checked integer default 0);
    """)


async def refresh(worker):
    from app.db import session
    from app.db.models import AgentJob, WorkItem
    from app.db.models.feature import FeatureScope
    db = worker.db
    roles = {r['agent_id']:name for name,r in worker.cfg.get('roles',{}).items() if r.get('chat_id') and r.get('agent_id')}
    if not roles:
        return
    where = (AgentJob.owner_id==worker.cfg['owner_id'],AgentJob.project==worker.cfg['project'],
             AgentJob.kind=='job',AgentJob.mode=='work',AgentJob.task_id.is_not(None),AgentJob.assignee_agent_id.in_(roles))
    query = select(AgentJob.id,AgentJob.task_id,AgentJob.assignee_agent_id,AgentJob.status,AgentJob.assignment_epoch,
                   AgentJob.origin_ref,WorkItem.title,WorkItem.agent_id).join(
                   WorkItem,WorkItem.id==AgentJob.task_id).join(FeatureScope,FeatureScope.task_id==WorkItem.id).where(
                   *where,WorkItem.owner_id==worker.cfg['owner_id'],FeatureScope.project==worker.cfg['project'],
                   AgentJob.assignment_epoch==FeatureScope.assignment_epoch)
    cursor = db.execute('select last_job from team_cursor where singleton=1').fetchone()
    async with session.AsyncSessionLocal() as owner:
        if cursor is None:
            # Enabling a group must not publish all of its historical assignments.
            latest = await owner.scalar(select(func.max(AgentJob.id)).where(*where))
            with db:
                db.execute('insert into team_cursor values(1,?)',(latest or 0,))
            return
        fresh = (await owner.execute(query.where(AgentJob.id>cursor[0]).order_by(AgentJob.id).limit(50))).all()
        tracked = db.execute("""select job,status from team_publications where status not in ('completed','cancelled','failed')
            order by checked,job limit 50""").fetchall()
        known = {x['job']:x['status'] for x in tracked}
        changed = (await owner.execute(query.where(AgentJob.id.in_(known)))).all() if known else []
    with db:
        if known:
            db.executemany('update team_publications set checked=unixepoch() where job=?',[(j,) for j in known])
        for job in [*fresh,*changed]:
            role = roles[job.assignee_agent_id]
            native = db.execute('select 1 from submissions where event=?',(job.origin_ref,)).fetchone()
            if native or job.status==known.get(job.id):
                continue
            title = job.title if job.assignee_agent_id==job.agent_id and job.assignment_epoch==1 else '项目协作任务'
            ensure_group_root(db,job.task_id,role,title)
            if job.id not in known:
                outgoing = enqueue(db,job.task_id,role,{'text':f'已收到派工 #{job.id}，任务 #{job.task_id}。当前状态：{STATUS.get(job.status,job.status)}。'})
            else:
                outgoing = enqueue(db,job.task_id,role,{'text':f'派工 #{job.id} 状态更新：{STATUS.get(job.status,job.status)}。'})
            bind_outgoing(db,outgoing,[job.id])
            db.execute('insert into team_publications(job,task,role,status) values(?,?,?,?) on conflict(job) do update set status=excluded.status',
                       (job.id,job.task_id,role,job.status))
        if fresh:
            db.execute('update team_cursor set last_job=? where singleton=1',(fresh[-1].id,))


async def poll(worker):
    now = time.monotonic()
    if now < getattr(worker,'next_team_poll',0):
        return
    worker.next_team_poll = now + 15
    try:
        from .approvals import collect_team_requests
        await collect_team_requests(worker)
        await refresh(worker)
    except SQLAlchemyError as error:
        logging.getLogger(__name__).warning('Team work status unavailable: %s',type(error).__name__)
