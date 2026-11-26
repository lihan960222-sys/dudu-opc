"""Steward-only suggestions. Every choice names one previously delivered offer."""
import json
import logging
import re
import time
from urllib.parse import quote

import httpx
from app.core.steward_timing import PATROL_SECONDS
from .routing import enqueue

CHOICES = {'采纳':'approve','稍后':'defer','忽略':'dismiss','跳过':'dismiss','later':'defer','skip':'dismiss'}
LOG = logging.getLogger(__name__)


def initialize(db):
    db.execute("""create table if not exists suggestion_offers(
        id text, revision integer, outgoing text unique not null, decision_event text,
        choice text, options text, state text not null default 'offered', primary key(id,revision))""")
    columns = {r[1] for r in db.execute("pragma table_info(suggestion_offers)")}
    if "digest" not in columns:
        db.execute("alter table suggestion_offers add column digest text")
    if "position" not in columns:
        db.execute("alter table suggestion_offers add column position integer")
    db.execute("""create table if not exists suggestion_delivery_clock(
        singleton integer primary key check(singleton=1), sent real not null)""")


def delivered(db, outgoing):
    """Called in the same transaction as the successful outgoing receipt."""
    db.execute("""insert or replace into suggestion_delivery_clock(singleton,sent)
        select 1,? from suggestion_offers where coalesce(digest,outgoing)=? limit 1""", (time.time(), outgoing))


def offered_options(item):
    return [option for option in item.get('options',[])
            if isinstance(option,dict) and option.get('id') in ('brief','followup') and isinstance(option.get('label'),str)][:2]


STATUS = {
    "有任务在等待": "等待中", "有成果等待验收": "待验收", "已过计划日期": "已延期",
    "交付条件尚未写清": "验收条件待定", "有可开始的任务尚无负责人": "待指派",
}


def digest_text(items):
    lines = []
    for n, item in enumerate(items, 1):
        prefix, sep, subject = item["title"].partition(" · ")
        title = subject if sep and prefix in STATUS else item["title"]
        title = " ".join(title.split())
        title = title if len(title) <= 52 else title[:51] + "…"
        lines.append(f"{n}. {title}：{STATUS.get(prefix, '待确认')}。")
    if len(items) == 1:
        options = offered_options(items[0])
        menu = "，".join(f"「{n}」{opt['label']}" for n, opt in enumerate(options,1))
        lines.append("老板，您可以引用这条消息，回复" + (menu or "「采纳」「稍后」或「忽略」") + "。")
    else:
        waiting = next((n for n, item in enumerate(items, 1)
                        if item["title"].startswith("有任务在等待 · ")
                        and any(o["id"] == "followup" for o in offered_options(item))), None)
        if waiting:
            lines.append(f"老板，第 {waiting} 项还在等。如果您要我查，引用回复「跟进{waiting}」就行。")
        else:
            lines.append("老板，如果您想让我跟进其中一项，可以引用回复「跟进」加序号。")
    return "\n".join(lines)


def poll(worker, *, wall_now=None):
    if not worker.cfg.get('proactive_suggestions',False):
        return
    now = time.monotonic()
    if now < getattr(worker,'next_offer_poll',0):
        return
    worker.next_offer_poll = now + 60
    # Reuse old journal timestamps on upgrade. If delivery had to retry, the
    # owner's quiet period starts when Feishu actually accepted the last offer.
    last, pending = worker.db.execute("""select max(mark),max(pending) from (
        select max(o.created) as mark,max(o.state='pending') as pending from suggestion_offers s
        join outbox o on o.id=coalesce(s.digest,s.outgoing)
        union all select sent,0 from suggestion_delivery_clock)""").fetchone()
    if pending:
        return
    if last is not None and (time.time() if wall_now is None else wall_now) < last + PATROL_SECONDS:
        return
    try:
        response = worker.api.get('steward/suggestions',timeout=5)
        response.raise_for_status()
        items = response.json()['items']
        fresh = []
        for item in items:
            if item.get('state') != 'pending':
                continue
            key = str(item['id']), int(item['revision'])
            if not worker.db.execute('select 1 from suggestion_offers where id=? and revision=?',key).fetchone():
                fresh.append(item)
            if len(fresh) == 3:
                break
        if fresh:
            with worker.db:
                outgoing = enqueue(worker.db, None, 'steward', {'text': digest_text(fresh)})
                for n, item in enumerate(fresh, 1):
                    worker.db.execute("""insert into suggestion_offers(id,revision,outgoing,digest,position,options)
                        values(?,?,?,?,?,?)""", (str(item['id']), int(item['revision']), outgoing if n == 1 else f"{outgoing}:{n}",
                                                outgoing, n, json.dumps(offered_options(item))))
    except (httpx.HTTPError,TypeError,ValueError,KeyError) as error:
        LOG.warning('Suggestion listing unavailable: %s',type(error).__name__)


def answer(worker,row,msg,text):
    if row['app']!=worker.cfg['steward']['app_id'] or msg.get('chat_type')!='p2p':
        return False
    db = worker.db
    offers = db.execute("""select s.* from suggestion_offers s join outbox o on o.id=coalesce(s.digest,s.outgoing)
        where o.role='steward' and o.state='sent' and o.remote=? order by s.position""",(msg.get('parent_id'),)).fetchall()
    choice = CHOICES.get(text.strip()) if msg.get('message_type')=='text' else None
    if not offers:
        if choice:
            worker.respond(row,'done','老板，我还没对上您说的那条建议。您方便引用原消息吗？')
        return bool(choice)
    option = None
    selection = text.strip() if msg.get('message_type')=='text' else ''
    if len(offers) > 1:
        match = re.fullmatch(r'(跟进|摘要|稍后|忽略)\s*([1-3])', selection)
        if not match or int(match[2]) > len(offers):
            worker.respond(row, 'done', '老板，我还没对上具体哪项。您可以引用原消息，回复「跟进」「摘要」「稍后」或「忽略」加序号。')
            return True
        offer = offers[int(match[2])-1]
        action = match[1]
        choice = CHOICES.get(action, 'approve')
        if choice == 'approve':
            available = json.loads(offer['options'] or '[]')
            option = 'followup' if action == '跟进' else 'brief'
            if not any(o['id'] == option for o in available):
                worker.respond(row, 'done', '老板，这项建议里没有您选的操作，我还没处理。')
                return True
    else:
        offer = offers[0]
        options = json.loads(offer['options'] or '[]')
        if selection in ('1','2') and int(selection)<=len(options):
            choice = 'approve'
            option = options[int(selection)-1]['id']
        elif choice=='approve' and options:
            option = options[0]['id']
    if not choice:
        worker.respond(row,'done','老板，我还没拿准您的选择。您可以引用这条建议，回复一个选项，也可以选「稍后」或「忽略」。')
        return True
    if offer['state']=='closed':
        if offer['decision_event']!=row['event']:
            worker.respond(row,'done','老板，我查到这条建议已经处理了。')
        return True
    if offer['decision_event'] and offer['decision_event']!=row['event']:
        worker.respond(row,'done','老板，我收到您的选择了，正在核对处理结果。')
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
            message = {'approve':'老板，我已按您的选择采纳。','defer':'老板，我已将这条建议留到稍后处理。','dismiss':'老板，我已按您的选择忽略这条建议。'}[choice]
            if result.get('report') and not result.get('followup_task_id'):
                message += '\n'+str(result['report'])
            if result.get('followup_task_id'):
                message += f"\n已建立跟进任务 #{result['followup_task_id']}。"

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
