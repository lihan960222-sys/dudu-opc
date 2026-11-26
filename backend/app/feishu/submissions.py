"""A persisted, immutable request for the backend's idempotent feature-turn endpoint."""
import hashlib
import json
from . import timing


def event_key(row):
    return hashlib.sha256((row['app']+'|'+row['event']).encode()).hexdigest()


def prepare(db,row,thread,role,title,prompt,ids):
    key = event_key(row)
    body = json.dumps({'feature_event_key':key,'prompt_text':prompt,'attachment_ids':ids},ensure_ascii=False)
    with db:
        timing.submitting(db,row)
        db.execute('insert into submissions values(?,?,?,?,?)',(key,thread,body,role,title))
        db.execute("update inbox set state='submitting' where app=? and event=?",(row['app'],row['event']))


async def adopt_withdrawn(worker, row, request, body):
    from app.db.session import AsyncSessionLocal
    from app.db.models import AgentJob, Event, FeatureMessage, Turn
    from app.services import feature_tasks
    from sqlalchemy import select
    key = event_key(row)
    thread = request['thread']
    async with AsyncSessionLocal() as session:
        message = await session.get(FeatureMessage,(worker.cfg["owner_id"],key))
        job = await session.get(AgentJob,message.result["job_id"]) if message and message.result.get("job_id") else None
        if (not job or job.owner_id != worker.cfg["owner_id"] or job.origin_ref != key
                or job.task_id != row["task"] or message.result.get("task_id") != job.task_id
                or job.assignee_thread_id != thread or job.assignee_agent_id != message.agent_id
                or await feature_tasks.assignment_current(session,job)):
            return False
        turn_id = message.result.get("submitted_turn_id") or job.assignee_turn_id
        ids = list(dict.fromkeys(body["attachment_ids"]))
        saved = message.result.get("submission_sha256")
        if saved and saved != hashlib.sha256(json.dumps([body["prompt_text"],ids],ensure_ascii=False).encode()).hexdigest():
            return False
        if turn_id:
            turn = await session.get(Turn,turn_id)
            if not turn or turn.thread_id != thread or turn_id != job.assignee_turn_id:
                return False
            if not saved:
                start = await session.scalar(select(Event.data).where(Event.turn_id==turn_id, Event.type=="turn.started"))
                if turn.prompt_text != body["prompt_text"] or [a["id"] for a in (start or {}).get("attachments",[])] != ids:
                    return False
    with worker.db:
        if turn_id:
            worker.db.execute("insert or ignore into executions values(?,?,?)",(key,thread,turn_id))
        worker.db.execute("update inbox set state=? where app=? and event=? and state='submitting'",
                          ("running" if turn_id else "superseded",row["app"],row["event"]))
    return True


async def send(worker,row):
    from app.db.session import AsyncSessionLocal
    from app.services import feature_tasks
    from .routing import enqueue
    key = event_key(row)
    request = worker.db.execute('select * from submissions where event=?',(key,)).fetchone()
    body = json.loads(request['body'])
    thread = request['thread']
    # The backend atomically checks the epoch and the immutable request on every
    # POST, including retries. Consult its old dispatch only after it refuses a stale submission.
    resp = worker.api.post(f'threads/{thread}/feature-turns',json=body)
    if resp.status_code == 409 and await adopt_withdrawn(worker,row,request,body):
        return
    resp.raise_for_status()
    turn_id = resp.json()['id']
    try:
        async with AsyncSessionLocal() as session:
            await feature_tasks.bind_turn(session,worker.cfg['owner_id'],key,thread,turn_id)
            await session.commit()
    except feature_tasks.DispatchConflict:
        if await adopt_withdrawn(worker,row,request,body):
            return
        raise
    with worker.db:
        timing.accepted(worker.db,row,resp.json())
        worker.db.execute('insert or ignore into executions values(?,?,?)',(key,thread,turn_id))
        change = worker.db.execute("update inbox set state='running', error=null where app=? and event=? and state='submitting'",
                                   (row['app'],row['event']))
        if change.rowcount:
            enqueue(worker.db,row['task'],request['role'],{'text':
                f"开始〈{request['title']}〉 #{row['task']}，已向会话 {thread} 提交 {len(body['attachment_ids'])} 个附件。"})
