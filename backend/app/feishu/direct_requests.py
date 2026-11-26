"""Immutable ordinary chat requests, replayed against the direct-turns endpoint only."""
import json
from . import timing
from .submissions import event_key
from .routing import parts


def source_reference(db, app, msg):
    """Map a quoted owner message or a sent DM reply within this app and chat."""
    chat = msg.get("chat_id")
    if msg.get("chat_type") != "p2p":
        return {}
    if chat:
        scope = "json_extract(i.payload,'$.chat_id')=?"
        params = (chat,)
    else:
        # Older Feishu DMs can omit the chat ID. Both incoming messages must then
        # have independent proof of the same authenticated tenant, application and owner.
        sender = db.execute("select owner,tenant from owner_dm_sources where app=? and message=? and chat is null",
                            (app,msg.get("message_id"))).fetchone()
        if not sender:
            return {}
        scope = """exists(select 1 from owner_dm_sources s where s.app=i.app and s.event=i.event
                   and s.message=i.message and s.owner=? and s.tenant=? and s.chat is null)"""
        params = tuple(sender)
    for key in (msg.get("parent_id"), msg.get("root_id")):
        if not key:
            continue
        prior = db.execute(f"""select i.app,i.event,i.task from inbox i
            where i.app=? and {scope} and json_extract(i.payload,'$.chat_type')='p2p'
            and (i.message=? or exists(select 1 from outbox o where o.role='steward' and o.state='sent' and o.remote=?
                and (json_extract(o.body,'$.reply_to')=i.message or exists(select 1 from direct_finals f
                    where f.app=i.app and f.event=i.event and f.outgoing=o.id))))
            and (i.task is not null or exists(select 1 from direct_turns d where d.app=i.app and d.event=i.event))
            limit 1""",(app,*params,key,key)).fetchone()
        if prior:
            return {"reference_event_key":event_key(prior), **({"reference_task_id":prior["task"]} if prior["task"] else {})}
        # Older task acknowledgements have a task id and no reply-to. Resolve
        # them only when this same authenticated conversation owns that task.
        legacy = db.execute(f"""select o.task from outbox o where o.remote=? and o.role='steward' and o.state='sent'
            and exists(select 1 from inbox i where i.task=o.task and i.app=?
            and {scope} and json_extract(i.payload,'$.chat_type')='p2p')
            limit 1""",(key,app,*params)).fetchone()
        if legacy:
            return {"reference_task_id":legacy[0]}
    return {}


def prepare(worker,row,thread,body):
    key=event_key(row)
    request={**body,'direct_event_key':key}
    stored = worker.db.execute("select payload from inbox where app=? and event=? and message=?",
                               (row["app"],row["event"],row["message"])).fetchone()
    msg = json.loads(stored[0] or "{}") if stored else {}
    if msg.get("message_type") and msg.get("content"):
        original_parts = parts(msg["message_type"],json.loads(msg["content"]))
        original = "\n".join(part["text"] for part in original_parts if part["kind"]=="text")
        if original and original != body["prompt_text"]:
            request["owner_text"] = original
        request.update(source_reference(worker.db, row["app"], msg))
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
