"""Process accepted DM events and reconcile upstream conversation turns."""
import asyncio
import hashlib
import json
import mimetypes
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from sqlalchemy.exc import SQLAlchemyError

from . import approvals, direct_requests, enquiries, recalls, suggestions, team_progress, timing, video_frames
from . import submissions
from .routing import enqueue, ensure_group_root, parts, task_requirements
from .artifacts import REPORT_REQUEST, parse_report, snapshot


def command(text):
    """An explicit first line makes selection deterministic across devices."""
    first, _, detail = text.strip().partition('\n')
    match = re.fullmatch(r'(新建|续接|查询)\s+(\S+)\s+(.+)', first)
    if not match:
        return None, None, '', text
    verb, role, title = match.groups()
    return {'新建':'independent','续接':'append','查询':'query'}[verb], role, title.strip(), detail or title


class Worker:
    def __init__(self, config, publisher):
        self.cfg, self.pub, self.db = config, publisher, publisher.db
        self.db.executescript("""
        create table if not exists retry(id text primary key, attempts integer, after integer);
        create table if not exists decisions(event text primary key, choice text, model text, digest text);
        create table if not exists submissions(event text primary key,thread integer,body text,role text,title text);
        create table if not exists questions(outgoing text primary key, app text, event text, resolved_by text);
        create table if not exists bindings(task integer primary key, role text, title text);
        create table if not exists executions(event text primary key, thread integer, turn integer);
        create table if not exists prepared(event text, position integer, attachment text, primary key(event,position));
        create table if not exists video_previews(event text,thread integer,ids text,primary key(event,thread));
        create table if not exists direct_requests(app text,event text,thread integer,body text,primary key(app,event));
        create table if not exists direct_turns(app text,event text,thread integer,turn integer,primary key(app,event));
        create table if not exists direct_notices(app text,event text,stage text,outgoing text,primary key(app,event,stage));
        """)
        approvals.initialize(self.db)
        recalls.initialize(self.db)
        suggestions.initialize(self.db)
        team_progress.initialize(self.db)
        url = config.get('backend','http://127.0.0.1:8000')
        if urlsplit(url).hostname not in ('127.0.0.1','localhost','::1'):
            raise ValueError('仅支持本机后端')
        headers = {}
        if config.get('backend_token_env'):
            headers['Authorization'] = 'Bearer ' + os.environ[config['backend_token_env']]
        self.api = httpx.Client(base_url=url.rstrip('/')+'/api/v1/',headers=headers,timeout=180,trust_env=False)

    def respond(self, row, state, text, task=None):
        with self.db:
            outgoing = enqueue(self.db,task,'steward',{'text':text})
            if state in ('clarify','done'):
                self.bind_final(row,outgoing)
            if state == 'clarify':
                self.db.execute('insert into questions(outgoing,app,event) values(?,?,?)',(outgoing,row['app'],row['event']))
            self.db.execute('update inbox set state=?,task=? where app=? and event=?',
                            (state,task,row['app'],row['event']))

    def reference(self, message):
        for field in ('parent_id','root_id'):
            key = message.get(field)
            if key:
                hit = self.db.execute("""select task from inbox where message=? and task is not null
                    union select task from outbox where remote=? and task is not null""",(key,key)).fetchone()
                if hit:
                    return hit[0]
        return None

    async def process(self,row):
        from app.db.session import AsyncSessionLocal
        from app.services import feature_tasks

        msg = json.loads(row['payload'])
        items = parts(msg['message_type'],json.loads(msg['content']))
        text = '\n'.join(p['text'] for p in items if p['kind']=='text')
        if row['app'] != self.cfg['steward']['app_id']:
            role = next((name for name,r in self.cfg['roles'].items() if r.get('app_id')==row['app'] and r.get('owner_open_id')),None)
            if role:
                await enquiries.answer(self,row,role,msg,text)
            else:
                with self.db:
                    self.db.execute("update inbox set state='ignored' where app=? and event=?",(row['app'],row['event']))
            return
        from . import documents,voice
        if await voice.handle(self,row,msg,text):
            return
        if await documents.handle(self,row,msg,text):
            return
        if suggestions.answer(self,row,msg,text) or approvals.answer(self, row, msg, text):
            return
        from . import history
        if await history.answer(self,row,msg,text):
            return
        if not self.cfg['roles'] and self.cfg.get('steward_thread'):
            await self.route_direct(row,msg,text)
            return
        intent, role, title, prompt = command(text)
        ref = self.reference(msg)
        parent = msg.get('parent_id') or msg.get('root_id')
        pending = self.db.execute("""select i.* from questions q join outbox o on o.id=q.outgoing
            join inbox i on i.app=q.app and i.event=q.event where o.remote=? and (i.state='clarify' or q.resolved_by=?)""",
            (parent,row['event'])).fetchone()
        source = row['message']
        if pending:
            original = json.loads(pending['payload'])
            original_items = parts(original['message_type'],json.loads(original['content']))
            selected = re.fullmatch(r'#(\d+)',text.strip())
            if selected:
                ref = int(selected[1])
                original_text = '\n'.join(p['text'] for p in original_items if p['kind']=='text')
                original_intent = command(original_text)[0]
                if original_intent in ('query','append'):
                    intent = original_intent
                else:
                    choice = self.db.execute("select choice from decisions where event=?",(pending['app']+'|'+pending['event'],)).fetchone()
                    if choice and (cached := json.loads(choice[0])):
                        intent = 'query' if cached[0]=='query' else 'append'
            if selected or intent:
                source = pending['message']
                items = original_items
                prompt = '\n'.join(p['text'] for p in items if 'text' in p) or '补充附件'
        if ref and intent != 'independent':
            binding = self.db.execute('select * from bindings where task=?',(ref,)).fetchone()
            if binding:
                title, role, intent = binding['title'], binding['role'], intent or 'append'
        if not intent and not ref and not pending and self.cfg.get('steward_thread'):
            is_direct_reply = self.db.execute("""select 1 from direct_turns d join inbox i using(app,event)
                where i.app=? and i.message=? union all select 1 from direct_finals f join outbox o on o.id=f.outgoing
                join direct_turns d on d.app=f.app and d.event=f.event where f.app=? and o.remote=? limit 1""",
                (row['app'],parent,row['app'],parent)).fetchone()
            casual = re.fullmatch(r'(你好|您好|早上好|晚上好|谢谢|辛苦了|在吗|你在吗|hi|hello)[！!.。？? ]*',text.strip(),re.I)
            if is_direct_reply or casual or not self.cfg.get('isolated_runner_enabled'):
                await self.route_direct(row,msg,text)
                return
            if self.cfg.get('owner_id') and self.cfg['steward'].get('agent_id'):
                from .lead_chat import select_thread
                from app.services.steward_deepseek import MODELS
                chosen,model=await select_thread(self.cfg,with_model=True)
                if chosen!=self.cfg['steward_thread'] or model in MODELS:
                    # The native lead can delegate. Do not send this owner's text
                    # to the separate Codex intent classifier after a vendor switch.
                    await self.route_direct(row,msg,text)
                    return
        if not intent and not ref and not pending and text.strip() and self.cfg.get('isolated_runner_enabled'):
            if self.direct_notice(row,'running','已收到，正在确认任务归属。'):
                self.flush(cleanup=False)
            from .steward import classify
            try:
                choice = await classify(self.cfg,self.db,row['app']+'|'+row['event'],text)
            except (ValueError,OSError,RuntimeError,asyncio.TimeoutError):
                choice = None
            if choice:
                intent,role,title,ref = choice
                if intent=='chat' and self.cfg.get('steward_thread'):
                    await self.route_direct(row,msg,text)
                    return
        if intent == 'independent' and ref:
            previous = self.db.execute('select role from bindings where task=?',(ref,)).fetchone()
            if not previous or previous['role']!=role:
                ref = None
        if role not in self.cfg['roles'] or not title:
            self.respond(row,'clarify','请回复本条并填写「#任务编号」或「新建 角色名 任务名」。原消息和附件会随任务保存。')
            return
        cfg = self.cfg['roles'][role]
        event_key = hashlib.sha256((row['app']+'|'+row['event']).encode()).hexdigest()
        async with AsyncSessionLocal() as session:
            result = await feature_tasks.route(session,owner_id=self.cfg['owner_id'],agent_id=cfg['agent_id'],
                project=self.cfg['project'],event_key=event_key,title=title,text=prompt or '补充附件',
                reference_id=ref,intent=intent,**task_requirements(prompt))
            await session.commit()
        task = result.get('task_id')
        if not task:
            self.respond(row,'clarify',f"请回复本条并填写「#任务编号」。候选任务：{result.get('candidates',[])}")
            return
        if intent == 'query':
            if pending:
                with self.db:
                    self.db.execute('update questions set resolved_by=? where app=? and event=?',(row['event'],pending['app'],pending['event']))
                    self.db.execute("update inbox set state='resolved',task=? where app=? and event=?",(task,pending['app'],pending['event']))
            self.respond(row,'done',f"〈{title}〉 #{task}：{result.get('status') or result.get('action')}",task)
            return
        with self.db:
            new = self.db.execute('insert or ignore into bindings values(?,?,?)',(task,role,title)).rowcount
            if new:
                ensure_group_root(self.db,task,role,title)
            if pending:
                self.db.execute('update questions set resolved_by=? where app=? and event=?',(row['event'],pending['app'],pending['event']))
                self.db.execute("update inbox set state='resolved',task=? where app=? and event=?",(task,pending['app'],pending['event']))
            if row['state']=='received':
                enqueue(self.db,task,'steward',{'text':f'已登记〈{title}〉 #{task}。引用本条可继续补充。'})
            self.db.execute("update inbox set task=?,state='bound' where app=? and event=?",(task,row['app'],row['event']))
        ahead = self.db.execute("""select 1 from inbox where task=? and state in ('submitting','running')
            and rowid < (select rowid from inbox where app=? and event=?) limit 1""",(task,row['app'],row['event'])).fetchone()
        if ahead:
            return
        files = [(i,p,self.pub.download(task,source,p)) for i,p in enumerate(items) if 'key' in p]
        if not self.cfg.get('isolated_runner_enabled'):
            self.respond(row,'held',f'任务 #{task} 已保存，收到 {len(files)} 个附件。执行环境尚未启用。',task)
            return
        async with AsyncSessionLocal() as session:
            model, workspace = await resolve_workspace(session,self.cfg,task,cfg)
            thread,busy = await feature_tasks.session_for(session,self.cfg['owner_id'],task,model=model,workspace_path=workspace)
            thread_id = thread.id
            if not busy:
                await feature_tasks.dispatch(session, owner_id=self.cfg['owner_id'],
                    steward_id=self.cfg['steward'].get('agent_id'), task_id=task,
                    event_key=event_key, thread_id=thread_id)
            await session.commit()
        if busy:
            return
        ids = []
        for i,part,path in files:
            prior = self.db.execute('select attachment from prepared where event=? and position=?',(event_key,i)).fetchone()
            if prior:
                ids.append(prior[0])
                continue
            mime = {'audio':'audio/ogg','media':'video/mp4'}.get(part['kind']) or mimetypes.guess_type(part['name'])[0] or 'application/octet-stream'
            name = part['name'] if '.' in part['name'] else part['name'] + {'audio':'.ogg','media':'.mp4'}.get(part['kind'],'')
            with path.open('rb') as stream:
                resp = self.api.post(f'threads/{thread_id}/attachments',files={'file':(name,stream,mime)},
                                     data={'consent_provider':'local'} if part['kind']=='audio' else None)
            resp.raise_for_status()
            ids.append(resp.json()['id'])
            with self.db:
                self.db.execute('insert into prepared values(?,?,?)',(event_key,i,ids[-1]))
        videos=[(p['name'],path) for _,p,path in files if p['kind']=='media']
        ids.extend(video_frames.add(self,event_key,thread_id,videos,ids,provider=thread.provider,
                                    image_count=sum(p['kind']=='image' for _,p,_ in files)))
        if files:
            layout = []
            attachment_number = 0
            for part in items:
                if part['kind'] == 'text':
                    layout.append(part['text'])
                else:
                    attachment_number += 1
                    layout.append(f"[附件 {attachment_number} · {part['name']}]")
            prompt += '\n原消息顺序：\n' + '\n'.join(layout)
        if any(p['kind']=='audio' for _,p,_ in files):
            prompt += '\n如转写中的数值、单位或否定词不清楚，请列出需要用户确认的原句。'
        submissions.prepare(self.db,row,thread_id,role,title,(prompt or '请检查所附材料。') + REPORT_REQUEST,ids)
        await submissions.send(self,self.db.execute('select * from inbox where app=? and event=?',
            (row['app'],row['event'])).fetchone())

    async def reconcile(self,row):
        from app.db.session import AsyncSessionLocal
        from app.services import feature_tasks
        key = hashlib.sha256((row['app']+'|'+row['event']).encode()).hexdigest()
        execution = self.db.execute('select * from executions where event=?',(key,)).fetchone()
        resp = self.api.get(f"threads/{execution['thread']}")
        resp.raise_for_status()
        turn = next(t for t in resp.json()['turns'] if t['id']==execution['turn'])
        if not turn.get('finished_at'):
            approvals.notify(self,execution,row['task'])
            return
        async with AsyncSessionLocal() as session:
            await feature_tasks.report(session, self.cfg['owner_id'], key, execution['thread'], execution['turn'])
            await session.commit()
        task = row['task']
        role = self.db.execute('select role from bindings where task=?',(task,)).fetchone()[0]
        summary, public, named = parse_report(turn.get('result') or turn['status'])
        artifacts = []
        issues = []
        if named and turn['status']=='succeeded':
            from app.services import agent_store
            async with AsyncSessionLocal() as session:
                agent = await agent_store.get_agent(session,self.cfg['roles'][role]['agent_id'])
                if not agent or agent.owner_id != self.cfg['owner_id'] or not agent.role_path:
                    raise ValueError('无法确认产物归属')
                from app.db.models import Thread
                bound = await session.get(Thread,execution['thread'])
                folder = Path(agent.role_path)
                if not bound or bound.agent_id != agent.id or bound.owner_id != self.cfg['owner_id'] or (
                        Path(bound.workspace_path) != folder/'任务'/str(int(task))):
                    raise ValueError('角色目录与执行记录不一致')
            for name in named:
                try:
                    artifacts.append(snapshot(folder,task,name,self.pub.directory))
                except (OSError,ValueError):
                    issues.append('有一项产物未通过路径、大小或文件状态核对。')
        with self.db:
            enqueue(self.db,task,role,{'text':f"任务 #{task} 本轮状态：{turn['status']}。已登记 {len(artifacts)} 个公开产物。{public}"})
            final = enqueue(self.db,task,'steward',{'text':f"任务 #{task}：{summary[:8000]}{''.join(issues)}"})
            self.bind_final(row,final)
            timing.finished(self.db,row,turn)
            for artifact in artifacts:
                for audience in (role,'steward'):
                    enqueue(self.db,task,audience,artifact)
            self.db.execute("update inbox set state='done' where app=? and event=?",(row['app'],row['event']))

    def flush(self, *, cleanup=True, history_id=None):
        with self.db:
            self.db.execute("""update outbox set state='superseded' where state='pending' and id in
                (select n.outgoing from direct_notices n join direct_finals f using(app,event))""")
        for row in self.db.execute("""select o.* from outbox o left join retry r on r.id=o.id
            where o.state='pending' and coalesce(r.after,0)<=unixepoch() and
            (o.role='steward' or o.is_root=1 or exists(select 1 from enquiry_replies q where q.outgoing=o.id) or exists
            (select 1 from outbox root where root.is_root=1 and root.role=o.role and root.task=o.task and root.state='sent'))
            and ((? is null and not exists(select 1 from history_reply_policy h where h.outgoing=o.id)) or o.id=?)
            order by o.rowid""",(history_id,history_id)).fetchall():
            with self.db:
                self.db.execute("update outbox set state='verify' where id=?",(row['id'],))
            try:
                remote = self.pub.deliver(row)
            except Exception as exc:
                with self.db:
                    if isinstance(exc,(httpx.ConnectError,httpx.ConnectTimeout)) or (
                            isinstance(exc,httpx.HTTPStatusError) and exc.response.status_code==429):
                        self.db.execute("""insert into retry values(?,1,unixepoch()+10) on conflict(id)
                            do update set attempts=attempts+1,after=unixepoch()+min(600,10*(attempts+1))""",(row['id'],))
                        self.db.execute("update outbox set state='pending' where id=?",(row['id'],))
                    self.db.execute('update outbox set error=? where id=?',(type(exc).__name__,row['id']))
                continue
            with self.db:
                self.db.execute("update outbox set state='sent',remote=? where id=?",(remote,row['id']))

                self.db.execute("update delivery_timing set sent=unixepoch('subsec') where outgoing=?",(row['id'],))
                self.db.execute('update notice_recalls set remote=? where outgoing=?',(remote,row['id']))
        if cleanup:
            recalls.flush(self.db,self.pub,self.cfg.get('steward',{}).get('app_id'))

    def bind_final(self,row,outgoing):
        self.db.execute('update delivery_timing set outgoing=? where app=? and event=?',(outgoing,row['app'],row['event']))
        self.db.execute("""insert into direct_finals(app,event,outgoing)
            select ?,?,? where exists(select 1 from direct_notices where app=? and event=?)
            on conflict(app,event) do update set outgoing=excluded.outgoing""",
            (row['app'],row['event'],outgoing,row['app'],row['event']))

    def direct_final(self, row, state, text, *, history_policy=None):
        keys = (row['app'],row['event'])
        with self.db:
            outgoing = enqueue(self.db,None,'steward',{'text':text,'reply_to':row['message']})
            if history_policy:
                self.db.execute('insert into history_reply_policy values(?,?,?,?,?,?)',(outgoing,*history_policy))
            self.db.execute('update delivery_timing set outgoing=? where app=? and event=?',(outgoing,*keys))
            self.db.execute('insert into direct_finals values(?,?,?) on conflict(app,event) do update set outgoing=excluded.outgoing',(*keys,outgoing))
            self.db.execute("update inbox set state=? where app=? and event=?",(state,*keys))

    def direct_notice(self, row, stage, text):
        with self.db:
            keys = (row['app'], row['event'])
            if self.db.execute('select 1 from direct_notices where app=? and event=? and stage=?', (*keys, stage)).fetchone():
                return
            self.db.execute("""update outbox set state='superseded' where state='pending' and id in
                (select outgoing from direct_notices where app=? and event=?)""", keys)
            outgoing = enqueue(self.db,None,'steward',{'text':text,'reply_to':row['message']})
            self.db.execute('insert into direct_notices values(?,?,?,?)', (*keys, stage, outgoing))
            self.db.execute('insert into notice_recalls(outgoing) values(?)',(outgoing,))
        return outgoing

    async def route_direct(self,row,msg,text):
        thread=self.cfg['steward_thread']
        if self.cfg.get('owner_id') and self.cfg['steward'].get('agent_id') and not self.db.execute(
                "select 1 from inbox where state in ('direct_running','direct_submitting')").fetchone():
            from .lead_chat import select_thread
            key=row['app'],row['event']
            prior=self.db.execute('select thread from direct_targets where app=? and event=?',key).fetchone()
            thread=prior[0] if prior else await select_thread(self.cfg)
            with self.db:
                self.db.execute('insert or ignore into direct_targets values(?,?,?)',(*key,thread))
        self.direct_chat(row,msg,text,thread=thread)

    def direct_chat(self, row, msg, text, *, thread=None):
        items = parts(msg['message_type'], json.loads(msg['content']))
        resources = [part for part in items if 'key' in part]
        if len(resources) > 8 or (not text.strip() and not resources):
            self.respond(row,'held','请发送文字或最多 8 个附件。')
            return
        thread = thread or self.cfg['steward_thread']
        if self.db.execute("select 1 from inbox where state in ('direct_running','direct_submitting')").fetchone():
            self.direct_notice(row,'queued','已排队，等上一条处理完就开始。')
            return
        if self.direct_notice(row,'running','思考中…'):
            self.flush(cleanup=False)
        ids = []
        videos = []
        source_key=submissions.event_key(row)
        for position,part in enumerate(resources):
            path = self.pub.download(-thread, row['message'], part)
            kind = part['kind']
            if kind=='media':
                videos.append((part['name'],path))
            prior=self.db.execute('select attachment from prepared where event=? and position=?',(source_key,position)).fetchone()
            if prior:
                ids.append(prior[0])
                continue
            name = part['name']
            if '.' not in name:
                name += {'audio':'.ogg','media':'.mp4'}.get(kind,'')
            mime = {'audio':'audio/ogg','media':'video/mp4'}.get(kind) or mimetypes.guess_type(name)[0] or 'application/octet-stream'
            with path.open('rb') as stream:
                upload = self.api.post(f'threads/{thread}/attachments',files={'file':(name,stream,mime)},
                                       data={'consent_provider':'local'} if kind=='audio' else None)
            upload.raise_for_status()
            ids.append(upload.json()['id'])
            with self.db:
                self.db.execute('insert into prepared values(?,?,?)',(source_key,position,ids[-1]))
        ids.extend(video_frames.add(self,submissions.event_key(row),thread,videos,ids,
                                    image_count=sum(p['kind']=='image' for p in resources)))
        body = {'prompt_text':text.strip() or '请查看附件并回复。','attachment_ids':ids}
        if self.cfg.get('steward_skills'):
            body['skills'] = self.cfg['steward_skills']
        direct_requests.prepare(self,row,thread,body)
        direct_requests.send(self,row)

    async def tick(self):
        from .lead_chat import publish_reports,register_report
        for row in self.db.execute("select i.*,d.thread,d.turn from inbox i join direct_turns d using(app,event) where i.state='direct_running'").fetchall():
            try:
                response = self.api.get(f"threads/{row['thread']}")
                response.raise_for_status()
                turn = next((t for t in response.json()['turns'] if t['id']==row['turn']),None)
                if turn and turn.get('finished_at'):
                    with self.db:
                        timing.finished(self.db,row,turn)
                    if not await register_report(self,row,turn):
                        self.direct_final(row,'done' if turn['status']=='succeeded' else 'held',
                                     turn.get('result') or turn.get('error') or '本轮已结束，请在本机查看详情。')
            except (httpx.HTTPError, SQLAlchemyError):
                continue
        for row in self.db.execute("select * from inbox where state in ('received','bound','submitting','running','suggestion_deciding','direct_submitting') and not exists (select 1 from retry where retry.id=inbox.app||'|'||inbox.event and retry.after>unixepoch()) order by rowid").fetchall():
            try:
                if row['state']=='direct_submitting':
                    direct_requests.send(self,row)
                elif row['state']=='submitting':
                    await submissions.send(self,row)
                elif row['state']=='running':
                    await self.reconcile(row)
                else:
                    await self.process(row)
            except Exception as exc:
                latest = self.db.execute('select * from inbox where app=? and event=?',(row['app'],row['event'])).fetchone()
                reason = type(exc).__name__
                if isinstance(exc,httpx.HTTPStatusError):
                    reason += f' HTTP {exc.response.status_code}'
                if latest['state'] in ('running','submitting','direct_submitting'):
                    # Neither phase may ever fall back to creating another dispatch.
                    with self.db:
                        retry_key = row['app']+'|'+row['event']
                        self.db.execute("""insert into retry values(?,1,unixepoch()+5) on conflict(id)
                            do update set attempts=attempts+1,after=unixepoch()+min(300,5*(attempts+1))""",(retry_key,))
                        self.db.execute('update inbox set error=? where app=? and event=?',(reason,row['app'],row['event']))
                        if not latest['error']:
                            enqueue(self.db,latest['task'],'steward',{'text':f'正在核对已登记的执行请求（{reason}）。'})
                    continue
                if row['app'] != self.cfg.get('steward',{}).get('app_id'):
                    with self.db:
                        self.db.execute('update inbox set error=? where app=? and event=?',(reason,row['app'],row['event']))
                        self.db.execute('insert into retry values(?,1,unixepoch()+15) on conflict(id) do update set attempts=attempts+1,after=unixepoch()+min(600,15*(attempts+1))',
                                        (row['app']+'|'+row['event'],))
                    continue
                state = 'verify' if latest['state']=='verify' else 'held'
                feedback=f'当前步骤需要核对（{reason}）。原消息已保存。'
                if isinstance(exc,httpx.HTTPStatusError) and exc.request.url.path.endswith('/attachments'):
                    try:
                        code=exc.response.json().get('detail',{}).get('error_code')
                    except (ValueError,AttributeError):
                        code=None
                    reason_cn = {'transcription_unavailable':'本机语音转写或音频校验工具尚不可用',
                                 'transcription_failed':'本机转写未成功','unsupported_type':'后端暂不能读取这一文件格式',
                                 'too_large':'附件超过后端接收上限'}.get(code,'后端尚不能处理这个附件')
                    feedback=f'已收到附件，原件保存在本机。{reason_cn}（{reason}），本轮未开始。'
                if state=='held' and self.db.execute('select 1 from direct_notices where app=? and event=?',(row['app'],row['event'])).fetchone():
                    self.direct_final(latest,state,feedback)
                else:
                    self.respond(latest,state,feedback,latest['task'])
                with self.db:
                    self.db.execute('update inbox set error=? where app=? and event=?',(reason,row['app'],row['event']))
        from .history import deliver_ready,resume_jobs
        try:
            await publish_reports(self)
        except SQLAlchemyError:
            pass  # Pending receipts are retried on the next poll.
        resume_jobs(self)
        await deliver_ready(self)
        self.flush()
        suggestions.poll(self)
        await team_progress.poll(self)

    async def run(self, wake=None):
        await team_progress.poll(self)
        while True:
            if wake is not None:
                wake.clear()
            await self.tick()
            if wake is None:
                await asyncio.sleep(2)
            else:
                await asyncio.to_thread(wake.wait,2)

async def resolve_workspace(session, config, task, cfg):
    from app.services import agent_store, role_workspace
    agent = await agent_store.get_agent(session,cfg['agent_id'])
    if not agent or agent.owner_id != config['owner_id'] or not agent.role_path:
        raise ValueError('请先配置本用户的角色目录')
    role_dir = role_workspace.checked_root(Path(agent.role_path))
    model = role_workspace.load(role_dir).model
    folder = role_dir / '任务' / str(int(task))
    if folder.resolve() != folder:
        raise ValueError('任务目录不能经过链接')
    folder.mkdir(parents=True,exist_ok=True)
    await session.rollback()
    return model, str(folder)
