"""Immutable ordinary chat requests, replayed against the direct-turns endpoint only."""
import json
from . import timing
from .submissions import event_key


def prepare(worker,row,thread,body):
    key=event_key(row)
    request={**body,'direct_event_key':key}
    with worker.db:
        timing.submitting(worker.db,row)
        worker.db.execute('insert into direct_requests values(?,?,?,?)',(row['app'],row['event'],thread,json.dumps(request)))
        worker.db.execute("update inbox set state='direct_submitting' where app=? and event=?",(row['app'],row['event']))


def send(worker,row):
    db=worker.db
    key=row['app'],row['event']
    record=db.execute('select thread,body from direct_requests where app=? and event=?',key).fetchone()
    response=worker.api.post(f"threads/{record['thread']}/direct-turns",json=json.loads(record['body']))
    response.raise_for_status()
    turn=response.json()
    with db:
        timing.accepted(db,row,turn)
        db.execute('insert or ignore into direct_turns values(?,?,?,?)',(*key,record['thread'],turn['id']))
        db.execute("update inbox set state='direct_running',error=null where app=? and event=?",key)
