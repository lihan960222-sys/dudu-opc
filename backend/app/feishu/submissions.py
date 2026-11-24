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


async def send(worker,row):
    from app.db.session import AsyncSessionLocal
    from app.services import feature_tasks
    from .routing import enqueue
    key = event_key(row)
    request = worker.db.execute('select * from submissions where event=?',(key,)).fetchone()
    body = json.loads(request['body'])
    thread = request['thread']
    resp = worker.api.post(f'threads/{thread}/feature-turns',json=body)
    resp.raise_for_status()
    turn_id = resp.json()['id']
    async with AsyncSessionLocal() as session:
        await feature_tasks.bind_turn(session,worker.cfg['owner_id'],key,thread,turn_id)
        await session.commit()
    with worker.db:
        timing.accepted(worker.db,row,resp.json())
        worker.db.execute('insert or ignore into executions values(?,?,?)',(key,thread,turn_id))
        change = worker.db.execute("update inbox set state='running', error=null where app=? and event=? and state='submitting'",
                                   (row['app'],row['event']))
        if change.rowcount:
            enqueue(worker.db,row['task'],request['role'],{'text':
                f"开始〈{request['title']}〉 #{row['task']}，已向会话 {thread} 提交 {len(body['attachment_ids'])} 个附件。"})
