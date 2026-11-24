"""Read an explicitly selected Feishu conversation once, following a verified owner DM."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re

import httpx

from .publisher import API, PlatformError, checked
from .routing import parts

PREFIX = re.compile(r'^(?:请(?:帮我)?|帮我)?(?:读取|查看|查阅|查一下|看看|读一下)\s*(?:飞书)?(?:聊天记录|会话历史|私聊记录|单聊记录|聊天历史)\s*[：:]?\s*')
NAME = re.compile(r'[\w·.\-]{1,40}')
CHAT = re.compile(r'oc_[A-Za-z0-9]{1,100}')
PEER = re.compile(r'ou_[A-Za-z0-9]{1,100}')
SCOPES={'im:chat:read','im:message:readonly','im:message.p2p_msg:get_as_user'}
TAIL = re.compile(r'(?:最近\s*(\d{1,2})\s*(小时|天))?\s*(?:的?\s*(\d{1,2})\s*条)?[。！？!?]?')
NATURAL = re.compile(r'^(?:请(?:帮我)?|帮我)?(?:查看|查阅|查一下|看看|读一下|读取)\s*(?:我[和与跟])?[「“]?([\w·.\-]{1,40}?)[」”]?\s*(最近\s*\d{1,2}\s*(?:小时|天))?\s*的?(?:飞书)?(?:聊天记录|会话历史|单聊记录)(.*)$')
REVIEW=re.compile(r'^(?:请(?:帮我)?|帮我)?(?:让([\w·.\-]{1,40}?))?(总结|整理|提炼)(?:一下)?我[和与跟][「“]?([\w·.\-]{1,40}?)[」”]?\s*(最近\s*\d{1,2}\s*(?:小时|天))?\s*的?(?:飞书)?(?:聊天记录|单聊记录|私聊记录)(.*)$')
GUIDE='例如「查看飞书聊天记录 小周 最近6小时 10条」。支持最近1到72小时、1到30条消息，可在末尾加“关键词：交付”。'
MAX_PAGES=3


def is_command(text):
    return bool(PREFIX.match(text.strip()) or NATURAL.fullmatch(text.strip()) or REVIEW.fullmatch(text.strip()))


def parse(text,sources):
    review=REVIEW.fullmatch(text.strip())
    natural=None if PREFIX.match(text.strip()) else NATURAL.fullmatch(text.strip())
    tail=(natural[1]+' '+(natural[2] or '')+' '+natural[3] if natural
          else PREFIX.sub('',text.strip(),count=1)).strip()
    if review:
        tail=(review[3]+' '+(review[4] or '')+' '+review[5]).strip()
    if not tail or tail in ('来源','有哪些','列表'):
        return None,'已选会话：'+('、'.join(sources) or '暂无。请先在本机登记会话。')+'\n'+GUIDE
    keyword=None
    if match:=re.search(r'\s+关键词[：:]\s*([^\r\n]{1,50})$',tail):
        keyword=match[1].strip()
        tail=tail[:match.start()].strip()
    for alias in sorted(sources,key=len,reverse=True):
        quoted=next((q for q in (f'「{alias}」',f'“{alias}”',alias) if tail.startswith(q)),None)
        if not quoted:
            continue
        args=tail[len(quoted):].strip()
        period=TAIL.fullmatch(args)
        if not period:
            continue
        hours=int(period[1] or 24)*(24 if period[2]=='天' else 1)
        count=int(period[3] or 10)
        if not (1<=hours<=72 and 1<=count<=30):
            return None,GUIDE
        return (alias,sources[alias],hours,count,keyword),None
    return None,'未找到匹配的已选会话，或时间、条数格式有误。'+GUIDE


def excerpt(item):
    kind=item.get('msg_type')
    if kind not in ('text','post'):
        return '［非文字消息］'
    try:
        content=json.loads(item['body']['content'])
        value=' '.join(p['text'] for p in parts(kind,content) if p['kind']=='text')
        return ' '.join(value.split())[:220] or '［无文字］'
    except (KeyError,TypeError,ValueError,AttributeError):
        return '［此消息无法显示］'


def headers(cfg,scopes):
    from .owner_oauth import user_headers
    try:
        return user_headers(cfg,required=scopes)
    except (ValueError,RuntimeError) as error:
        raise ValueError(str(error)) from None


def peer_for(client,auth,chat,owner_open_id):
    if not isinstance(chat,str) or not CHAT.fullmatch(chat):
        raise ValueError('请输入有效的单聊会话 ID。')
    try:
        response=client.post(API+'/im/v1/chats/batch_query',headers=auth,params={'user_id_type':'open_id'},
                             json={'chat_ids':[chat]},timeout=6)
        rows=checked(response)['data']['items']
        if len(rows)==1:
            item=rows[0]
            peer=item.get('p2p_target_id')
            if (item.get('chat_id')==chat and item.get('chat_mode')=='p2p' and item.get('p2p_target_type')=='user'
                    and isinstance(peer,str) and PEER.fullmatch(peer) and peer!=owner_open_id):
                return peer
    except (httpx.HTTPError,PlatformError,KeyError,TypeError,AttributeError):
        raise ValueError('飞书未能核对所选会话，请检查个人授权和会话编号。') from None
    raise ValueError('这不是当前老板与一位可核实同事的一对一会话。')


def resolve_peer(cfg,chat_id):
    auth=headers(cfg,{'im:chat:read'})
    with httpx.Client() as client:
        return peer_for(client,auth,chat_id,cfg['steward']['open_id'])


async def reviewer(cfg,source,name):
    from app.db.session import AsyncSessionLocal
    from app.services import role_workspace
    from app.team.service import team_for,member,TeamError
    async with AsyncSessionLocal() as db:
        team=await team_for(db,cfg['owner_id'])
        if not team or team.lead_agent_id!=cfg['steward']['agent_id']:
            raise ValueError('请在团队设置中核对管家。')
        agent_id=source.get('agent_id') if name else team.lead_agent_id
        if not agent_id:
            raise ValueError('尚未为这位联系人指定该员工。')
        try:
            agent=await member(db,cfg['owner_id'],agent_id,team_id=team.id)
        except TeamError:
            raise ValueError('指定员工已停用或已离开团队。') from None
        if name and name!=agent.name:
            raise ValueError('所点员工与该联系人的授权员工不符。')
        model=agent.model
        if agent.role_path:
            role=role_workspace.load(Path(agent.role_path))
            model=role.model
        return agent.id,agent.name,agent.provider,model,agent.title or ''


def digest(entries):
    """Extract locally; no private text goes to a model, worker, or third-party service."""
    groups={'涉及决定或约定':[], '涉及后续安排':[], '待核实的问题':[], '其他发言':[]}
    for line in entries:
        if any(word in line for word in ('？','?','请问','是否')):
            label='待核实的问题'
        elif any(word in line for word in ('已确认','同意','已决定','已完成','确定')):
            label='涉及决定或约定'
        elif any(word in line for word in ('需要','请在','负责','计划','截止','明天','下周')):
            label='涉及后续安排'
        else:
            label='其他发言'
        if len(groups[label])<3:
            groups[label].append(line)
    return '\n'.join(label+'\n'+'\n'.join(lines) for label,lines in groups.items() if lines)


def finish_job(worker,row,job_id,state,matched,message,policy=None,notes=None):
    with worker.db:
        worker.db.execute('update private_history_jobs set state=?,matched=? where id=?',(state,matched,job_id))
        if notes and state=='codex_completed':
            worker.db.execute('insert or replace into private_summary_notes values(?,?,?)',(row['app'],row['event'],notes))
        worker.direct_final(row,'done',message,history_policy=policy)


def policy(alias,source,staff,profile,cloud=False):
    return alias,json.dumps(source),staff,profile[0] if profile else None,int(cloud)


async def current_policy(worker,row):
    from .workspace_access import settings_for
    try:
        settings=settings_for(worker.cfg)
        source=json.loads(row['source'])
        if settings['history_sources'].get(row['alias'])!=source or (row['cloud'] and settings.get('allow_codex_summary') is not True):
            return False
        if row['actor'] is not None and (await reviewer(worker.cfg,source,row['staff']))[0]!=row['actor']:
            return False
        headers(worker.cfg,SCOPES)
        return True
    except (ValueError,KeyError,TypeError,RuntimeError,OSError):
        return False


async def quoted_notes(worker,row,msg):
    steward=worker.cfg['steward']
    parent=msg.get('parent_id')
    if row['app']!=steward['app_id'] or msg.get('chat_type')!='p2p' or not parent or not msg.get('chat_id'):
        return ''
    source=worker.db.execute("""select n.notes,h.*,i.payload from owner_readwrite_requests now
        join private_summary_notes n on n.app=now.app join private_history_jobs j on (j.app,j.event)=(n.app,n.event)
        join owner_readwrite_requests original on (original.app,original.event)=(n.app,n.event)
        join inbox i on (i.app,i.event)=(n.app,n.event) join direct_finals f on (f.app,f.event)=(n.app,n.event)
        join outbox o on o.id=f.outgoing join history_reply_policy h on h.outgoing=o.id
        where now.app=? and now.event=? and now.message=? and now.owner=? and now.tenant=?
        and (original.owner,original.tenant)=(now.owner,now.tenant) and j.state='codex_completed'
        and i.state='done' and o.role='steward' and o.task is null and o.state='sent' and o.remote=?""",
        (row['app'],row['event'],row['message'],steward.get('open_id'),steward.get('tenant_key'),parent)).fetchone()
    if source:
        previous=json.loads(source['payload'])
        if previous.get('chat_type')=='p2p' and previous.get('chat_id')==msg['chat_id'] and await current_policy(worker,source):
            return source['notes']
    return ''


async def deliver_ready(worker):
    for row in worker.db.execute("""select h.*,o.body from history_reply_policy h join outbox o on o.id=h.outgoing
            left join retry r on r.id=o.id where o.state='pending' and coalesce(r.after,0)<=unixepoch()
            order by o.rowid limit 12""").fetchall():
        if not await current_policy(worker,row):
            body=json.loads(row['body'])
            body['text']='本次私聊查阅的授权已变更，结果已丢弃。'
            with worker.db:
                worker.db.execute("update outbox set body=? where id=?",(json.dumps(body),row['outgoing']))
                worker.db.execute("""delete from private_summary_notes where (app,event) in
                    (select app,event from direct_finals where outgoing=?)""",(row['outgoing'],))
        worker.flush(cleanup=False,history_id=row['outgoing'])


async def cloud_job(worker,row,job_id,alias,source,profile,entries,staff,local):
    from .private_summary import analyze
    from .workspace_access import settings_for
    async def authorized():
        current=settings_for(worker.cfg)
        if current.get('allow_codex_summary') is not True or current['history_sources'].get(alias)!=source:
            return False
        headers(worker.cfg,SCOPES)
        return await reviewer(worker.cfg,source,staff)==profile
    try:
        points,notes=await analyze(profile,entries,authorized)
        message=f'私密任务 #{job_id} · {profile[1]} · OpenAI Codex 摘要\n与「{alias}」的本次单聊摘录\n'+points
        state='codex_completed'
    except (ValueError,KeyError,TypeError,RuntimeError,OSError,TimeoutError):
        notes=None
        state='local_completed'
        message=local+'\n此次 Codex 摘要未完成，以上为本机整理。'
    try:
        permitted=await authorized()
    except (ValueError,KeyError,TypeError,RuntimeError,OSError):
        permitted=False
    if not permitted:
        finish_job(worker,row,job_id,'revoked',0,'本次私密整理的授权已变更，结果已丢弃。')
    else:
        finish_job(worker,row,job_id,state,len(entries),message,policy(alias,source,staff,profile,True),notes)


def resume_jobs(worker):
    active=getattr(worker,'private_summary_tasks',{})
    for job in worker.db.execute("""select j.id,i.* from private_history_jobs j join inbox i using(app,event)
            where j.state='analyzing' and i.state='private_summarizing'""").fetchall():
        if job['id'] not in active:
            finish_job(worker,job,job['id'],'interrupted',0,'私密摘要在网关重启时中断。若仍需要这份摘要，请重新发起请求。')

def read(publisher,auth,chat,hours,count,end,*,owner,peer,alias):
    start=end-hours*3600
    seen=set()
    tokens=set()
    result=[]
    params={'container_id_type':'chat','container_id':chat,'start_time':str(start),
            'end_time':str(end),'sort_type':'ByCreateTimeDesc','page_size':min(10,count)}
    for _ in range(MAX_PAGES):
        response=publisher.http.get(API+'/im/v1/messages',headers=auth,params=params,timeout=6)
        page=checked(response)['data']
        for item in page.get('items',[]):
            key=item.get('message_id')
            if item.get('chat_id')!=chat or item.get('deleted') or not key or key in seen:
                continue
            try:
                created=int(item['create_time'])//1000
            except (KeyError,TypeError,ValueError):
                continue
            if not start<=created<=end:
                continue
            sender=item.get('sender') or {}
            who=sender.get('id')
            if sender.get('sender_type')!='user' or who not in (owner,peer):
                continue
            seen.add(key)
            stamp=datetime.fromtimestamp(created,timezone.utc).astimezone().strftime('%m-%d %H:%M %z')
            label='我' if who==owner else alias
            result.append(f'{stamp}  {label}：{excerpt(item)}')
            if len(result)==count:
                break
        token=page.get('page_token')
        if len(result)==count or not page.get('has_more') or not token or token in tokens:
            break
        tokens.add(token)
        params['page_token']=token
    return result


async def answer(worker,row,msg,text):
    if msg.get('message_type')!='text' or not is_command(text):
        return False
    identity=worker.cfg['steward']
    proof=worker.db.execute("""select t.received from owner_readwrite_requests h join delivery_timing t using(app,event)
        where h.app=? and h.event=? and h.message=? and h.owner=? and h.tenant=?""",
        (row['app'],row['event'],row['message'],identity.get('open_id'),identity.get('tenant_key'))).fetchone()
    if not (row['app']==identity['app_id'] and msg.get('chat_type')=='p2p' and proof):
        with worker.db:
            worker.db.execute("update inbox set state='ignored' where app=? and event=?",(row['app'],row['event']))
        return True
    current=worker.db.execute('select state from inbox where app=? and event=?',(row['app'],row['event'])).fetchone()
    if not current or current[0]!='received':
        return True
    from .workspace_access import settings_for
    sources={k:v for k,v in settings_for(worker.cfg)['history_sources'].items()
             if NAME.fullmatch(k) and isinstance(v,dict) and CHAT.fullmatch(v.get('chat_id') or '')
             and PEER.fullmatch(v.get('peer_open_id') or '')}
    request,message=parse(text,sources)
    job=None
    job_state='failed'
    matched=0
    result_policy=None
    if request:
        alias,source,hours,count,keyword=request
        try:
            review=REVIEW.fullmatch(text.strip())
            profile=await reviewer(worker.cfg,source,review[1]) if review else None
            end=int(proof['received'])
            if profile:
                key=(row['app'],row['event'])
                with worker.db:
                    worker.db.execute("""insert or ignore into private_history_jobs
                        (app,event,alias,chat,peer,assignee,start,end,cap) values(?,?,?,?,?,?,?,?,?)""",
                        (*key,alias,source['chat_id'],source['peer_open_id'],profile[0],end-hours*3600,end,count))
                    job=worker.db.execute('select * from private_history_jobs where app=? and event=?',key).fetchone()
                if (job['alias'],job['chat'],job['peer'],job['assignee'])!=(alias,source['chat_id'],source['peer_open_id'],profile[0]):
                    raise ValueError('联系人或员工授权已变更，请重新发起整理。')
            auth=headers(worker.cfg,SCOPES)
            chat=source['chat_id']
            if peer_for(worker.pub.http,auth,chat,identity['open_id'])!=source['peer_open_id']:
                raise ValueError('会话对方与本机登记的同事不符，请在老板设置中核对。')
            entries=read(worker.pub,auth,chat,hours,count,end,owner=identity['open_id'],peer=source['peer_open_id'],alias=alias)
            if keyword:
                entries=[line for line in entries if keyword.casefold() in line.casefold()]
            message=(f'「{alias}」最近{hours}小时的可见记录（最多查阅{count}条，较新在前）'
                     +(f'，关键词「{keyword}」' if keyword else '')+'\n'
                     +('\n'.join(entries) if entries else '在本次查询范围内没有找到可显示的消息。'))
            if settings_for(worker.cfg)['history_sources'].get(alias)!=source:
                raise ValueError('联系人授权已变更，请重新发起查询。')
            headers(worker.cfg,SCOPES)
            if profile:
                profile=await reviewer(worker.cfg,source,review[1])
                message=(f'私密任务 #{job["id"]} · {profile[1]} · 本机摘录整理\n'
                         f'与「{alias}」最近{hours}小时的单聊\n'+(digest(entries) if entries else '未找到可显示的记录。'))
                job_state,matched='completed',len(entries)
                if entries and settings_for(worker.cfg).get('allow_codex_summary') is True:
                    import asyncio
                    if not hasattr(worker,'private_summary_tasks'):
                        worker.private_summary_tasks={}
                    if worker.private_summary_tasks:
                        message+='\n另一项私密摘要正在进行，本次仅在本机整理。'
                    else:
                        with worker.db:
                            worker.db.execute("update private_history_jobs set state='analyzing' where id=?",(job['id'],))
                            worker.db.execute("update inbox set state='private_summarizing' where app=? and event=?",
                                              (row['app'],row['event']))
                        worker.direct_notice(row,'running','已收到，正在整理指定的单聊。')
                        future=asyncio.create_task(cloud_job(worker,row,job['id'],alias,source,profile,entries,review[1],message))
                        worker.private_summary_tasks[job['id']]=future
                        future.add_done_callback(lambda _:worker.private_summary_tasks.pop(job['id'],None))
                        return True
            if entries:
                result_policy=policy(alias,source,review[1] if review else None,profile)
        except ValueError as error:
            message=f'无法读取「{alias}」。'+str(error)[:240]
        except (httpx.HTTPError,PlatformError,KeyError,TypeError):
            message=f'暂时无法读取「{alias}」。请核对个人授权和单聊权限，或稍后再试。'
    with worker.db:
        if job:
            worker.db.execute('update private_history_jobs set state=?,matched=? where id=?',(job_state,matched,job['id']))
        worker.direct_final(row,'done',message,history_policy=result_policy)
    return True


def main():
    parser=argparse.ArgumentParser(description='本机维护管家可按次查阅的飞书会话')
    parser.add_argument('--config',default=os.getenv('FEISHU_CONFIG'),required=not os.getenv('FEISHU_CONFIG'))
    actions=parser.add_mutually_exclusive_group(required=True)
    actions.add_argument('--add',nargs=2,metavar=('别名','CHAT_ID'))
    actions.add_argument('--remove',metavar='别名')
    args=parser.parse_args()
    path=Path(args.config).resolve(strict=True)
    from .workspace_access import load_config,settings_for,save_settings
    cfg=load_config(path)
    current=settings_for(cfg)
    sources=dict(current['history_sources'])
    if args.add:
        alias,chat=args.add
        if not NAME.fullmatch(alias) or not CHAT.fullmatch(chat) or alias in ('列表','来源','有哪些'):
            parser.error('请使用1至40个汉字、字母、数字或短横线作为别名，chat_id 以 oc_ 开头。')
        if alias in sources and sources[alias]['chat_id']!=chat:
            parser.error('别名已关联其他会话，请先移除旧来源。')
        try:
            peer=resolve_peer(cfg,chat)
        except ValueError as error:
            parser.error(str(error))
        sources[alias]={'chat_id':chat,'peer_open_id':peer,'agent_id':sources.get(alias,{}).get('agent_id')}
    else:
        if args.remove not in sources:
            parser.error('此来源尚未登记。')
        del sources[args.remove]
    save_settings(path,history_sources=sources,expected_revision=current['revision'])
    print('联系人设置已保存，下次查询立即生效。')


if __name__=='__main__':
    main()
