"""Steward-only suggestions. Every choice names one previously delivered offer."""
import json
import logging
import time
from urllib.parse import quote

import httpx
from .routing import enqueue

CHOICES = {'采纳':'approve','稍后':'defer','忽略':'dismiss','跳过':'dismiss','later':'defer','skip':'dismiss'}
LOG = logging.getLogger(__name__)


def initialize(db):
    db.execute("""create table if not exists suggestion_offers(
        id text, revision integer, outgoing text unique not null, decision_event text,
        choice text, options text, state text not null default 'offered', primary key(id,revision))""")


def offered_options(item):
    return [option for option in item.get('options',[])
            if isinstance(option,dict) and option.get('id') in ('brief','followup') and isinstance(option.get('label'),str)][:2]


def poll(worker):
    if not worker.cfg.get('proactive_suggestions',False):
        return
    now = time.monotonic()
    if now < getattr(worker,'next_offer_poll',0):
        return
    worker.next_offer_poll = now + 60
    try:
        response = worker.api.get('steward/suggestions',timeout=5)
        response.raise_for_status()
        items = response.json()['items']
        new = 0
        for item in items:
            if item.get('state')!='pending':
                continue
            key = str(item['id']), int(item['revision'])
            if worker.db.execute('select 1 from suggestion_offers where id=? and revision=?',key).fetchone():
                continue
            with worker.db:
                options = offered_options(item)
                menu = '\n'.join(f"{n}. {opt['label'][:180]}" for n,opt in enumerate(options,1))
                guide = '引用本条回复选项序号，或回复「稍后」「忽略」。' if options else '引用本条回复「采纳」「稍后」或「忽略」。'
                text = f"嘟嘟的建议：{item['title'][:200]}\n{item['detail'][:1600]}\n{menu}\n{guide}"
                outgoing = enqueue(worker.db,item.get('task_id'),'steward',{'text':text})
                worker.db.execute('insert into suggestion_offers(id,revision,outgoing,options) values(?,?,?,?)',(*key,outgoing,json.dumps(options)))
            new += 1
            if new >= 3:
                break
    except (httpx.HTTPError,TypeError,ValueError,KeyError) as error:
        LOG.warning('Suggestion listing unavailable: %s',type(error).__name__)


def answer(worker,row,msg,text):
    if row['app']!=worker.cfg['steward']['app_id'] or msg.get('chat_type')!='p2p':
        return False
    db = worker.db
    offer = db.execute("""select s.* from suggestion_offers s join outbox o on o.id=s.outgoing
        where o.role='steward' and o.state='sent' and o.remote=?""",(msg.get('parent_id'),)).fetchone()
    choice = CHOICES.get(text.strip()) if msg.get('message_type')=='text' else None
    if not offer:
        if choice:
            worker.respond(row,'done','请直接引用嘟嘟发来的那条建议，再选择如何处理。')
        return bool(choice)
    options = json.loads(offer['options'] or '[]')
    selection = text.strip()
    option = None
    if msg.get('message_type')=='text' and selection in ('1','2') and int(selection)<=len(options):
        choice = 'approve'
        option = options[int(selection)-1]['id']
    elif choice=='approve' and options:
        option = options[0]['id']
    if not choice:
        worker.respond(row,'done','请引用这条建议，回复其中一个选项，或选择「稍后」「忽略」。')
        return True
    if offer['state']=='closed':
        if offer['decision_event']!=row['event']:
            worker.respond(row,'done','这条建议已处理。')
        return True
    if offer['decision_event'] and offer['decision_event']!=row['event']:
        worker.respond(row,'done','嘟嘟已经收到这条建议的选择，正在核对处理结果。')
        return True
    with db:
        db.execute("update suggestion_offers set state='deciding',choice=?,decision_event=? where outgoing=?",
                   (choice,row['event'],offer['outgoing']))
        db.execute("update inbox set state='suggestion_deciding' where app=? and event=?",(row['app'],row['event']))
    url = 'steward/suggestions/'+quote(offer['id'],safe='')+'/decision'
    try:
        body={'choice':choice,'revision':offer['revision']}
        if option:
            body['option']=option
        resp = worker.api.post(url,json=body,timeout=5)
        if resp.status_code==409:
            current = worker.api.get('steward/suggestions/'+quote(offer['id'],safe=''),timeout=5)
            if current.status_code==404:
                message = '这条建议已移除。'
            else:
                current.raise_for_status()
                state = current.json().get('state')
                label = {'approved':'已采纳','deferred':'已稍后处理','dismissed':'已忽略','pending':'内容已更新'}.get(state,'已更新')
                message = f'原建议已变更。当前记录：{label}。'
        elif resp.status_code==404:
            message = '这条建议已移除。'
        else:
            resp.raise_for_status()
            result = resp.json()
            message = {'approve':'已采纳。','defer':'已选择稍后处理。','dismiss':'已忽略这条建议。'}[choice]
            if result.get('report'):
                message += '\n'+str(result['report'])[:3500]
            if result.get('followup_task_id'):
                message += f"\n后续任务 #{result['followup_task_id']}。"

    except httpx.HTTPError as error:
        with db:
            db.execute('insert into retry values(?,1,unixepoch()+15) on conflict(id) do update set attempts=attempts+1,after=unixepoch()+min(600,15*(attempts+1))',
                       (row['app']+'|'+row['event'],))
            db.execute('update inbox set error=? where app=? and event=?',(type(error).__name__,row['app'],row['event']))
        return True
    with db:
        db.execute("update suggestion_offers set state='closed' where outgoing=?",(offer['outgoing'],))
        enqueue(db,None,'steward',{'text':message,'reply_to':row['message']})
        db.execute("update inbox set state='done',error=null where app=? and event=?",(row['app'],row['event']))
    return True
