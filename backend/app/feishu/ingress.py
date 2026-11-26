"""Accept authenticated owner messages for a single application and destination."""
import json
import time
import re


def group_topic(db,identity,message):
    role=identity.get('enquiry_role')
    if not role or not identity.get('chat_id') or message.get('chat_id')!=identity.get('chat_id') or message.get('message_type')!='text':
        return None
    def published(remote):
        return db.execute("""select o.task,o.is_root from outbox o where o.remote=?
            and o.role=? and o.state='sent' and (not exists
            (select 1 from enquiry_replies r where r.outgoing=o.id) or exists
            (select 1 from enquiry_replies r join group_enquiries g using(app,event)
            where r.outgoing=o.id and g.chat=?))""",(remote,role,identity['chat_id'])).fetchone()
    parent=published(message.get('parent_id'))
    own_open=identity.get('bot_open_id')
    mentioned=own_open and any(m.get('id',{}).get('open_id')==own_open for m in message.get('mentions',[]))
    if not (parent or mentioned):
        return None
    root=published(message.get('root_id'))
    if root and not root['is_root']:
        return None
    target=root or parent
    if not target or parent and root and parent['task']!=root['task']:
        return None
    return db.execute("""select task,remote from outbox where task=? and role=? and is_root=1 and state='sent'""",
                      (target['task'],role)).fetchone()


FULL_PUBLICATION = re.compile(
    r"(?:请)?(?:你)?(?:继续)?(?:让他(?:继续)?(?:干|做))?[，,。\s]*"
    r"(?:看|读取|查看|整理)(?:一下)?(?:全部|所有)(?:的)?(?:消息|聊天)记录[，,。\s]*"
    r"(?:并|然后|再)?(?:整理(?:成)?文档)?(?:落入|存入|写入|放到|保存到|存到)(?:飞书)?云盘[。！!\s]*"
)


# Only turn affirmative delivery instructions into actions. The body is opaque.
DOC_TARGET = r"(?:飞书(?:云)?文档|(?:飞书)?云盘)"
DOC_ACTION = re.compile(
    r"(?:整理|总结|汇总|生成|输出|导出|落实|写|保存|存|放|落|上传|发布|创建|新建|做)(?:成|入|进|到|为|在)?(?:我指定的|指定的)?(?:一份|一篇|一个)?"
    + DOC_TARGET
)
DOC_REQUIREMENT = re.compile(r"(?:交付物|交付形式|交付格式|最终产物|最终成果)(?:要求)?(?:是|用|为|要用|必须是)" + DOC_TARGET)
DOC_NEGATIVE = re.compile(r"(?:不要|不用|无需|别|禁止|停止|暂缓|取消|不必|不允许)(?:再)?(?:整理|创建|发布|写|上传|生成|做|交付|导出|保存|存|放|落)|先不(?:写|上传|发布|保存)|只读(?:就好|即可)")
DOC_QUESTION = re.compile(r"如何|为什么|怎么|是否|能否|能不能|有没有|能做什么|[？?]|吗[。！! ]*$")
DOC_PREFIX = re.compile(r"^(?:请|麻烦|帮我|把|将|让|叫|你|给我|写|新建|创建|整理|导出|输出|生成|保存|发布|上传|存|放|总结|汇总|做|只需要|需要|最后要|最终要)")
DOC_CONTACT = re.compile(r"我[与和跟][「“]?([\w·.\-]{1,40}?)[」”]?的(?:全部|所有)?(?:飞书)?(?:聊天记录|聊天内容|单聊记录|私聊记录)")


def revision_request(text):
    """Recognize an instruction to continue an existing private knowledge task."""
    if not isinstance(text,str):
        return False
    instruction = text.strip().partition("\n")[0]
    if re.search(r"不要(?:再)?(?:更新|整理|修改)|取消(?:更新|整理)|如何|怎么做|能否|是否|[？?]",instruction):
        return False
    resume = re.search(r"(?:继续|接着|重新|再)(?:帮我)?(?:整理|归纳|看一下|看一看|检查|更新|汇总|归档|处理)|(?:补充|更新)(?:一下)?(?:原来|之前|刚才|上次|这份|那份)的?(?:知识库|文档)",instruction)
    source = re.search(r"消息|聊天|单聊|知识库|文档|会话",instruction)
    editor = re.search(r"二狗|继续|接着|之前|上次|原来|刚才|这份|那份",instruction)
    update = re.search(r"整理|归纳|汇总|分类|归档|补充|更新(?:一下)?(?:原|这|那|之前|上次|知识库|文档)",instruction)
    return bool(resume and source and editor and update and not re.search(r"不要(?:再)?(?:写入|写|存入|上传|发布)|只读|只看不改",instruction))


def structured_revision(text):
    return bool(re.search(r"分类|归类|摘要|提炼|合并|分析|结构|markdown|md格式|按主题",text,re.I))


def is_delivery_request(text):
    first = text.strip().split("\n",1)[0]
    first = re.sub(r"[，,]\s*(?:可以吗|好吗|行吗)[？?。]?\s*$","",first)
    if DOC_NEGATIVE.search(first) or DOC_QUESTION.search(first):
        return False
    if FULL_PUBLICATION.fullmatch(first) or revision_request(text):
        return True
    for clause in re.split(r"[，,。；;]",first):
        clause = clause.strip()
        if re.search(r"(?:不要|请勿|别把|不需要|不必|不用|禁止|暂不|不想).*(?:飞书文档|云盘)",clause):
            continue
        if re.search(r"(?:飞书(?:云)?文档|云盘)的(?:方法|流程|步骤|教程|含义|权限)",clause):
            continue
        if DOC_REQUIREMENT.match(clause):
            return True
        if DOC_ACTION.search(clause) and (DOC_PREFIX.match(clause)
                or re.search(r"(?:最后|最终)(?:需要|要|必须)",clause)):
            return True
    return False


def document_assignment(text):
    first, _, body = text.strip().partition("\n")
    source = DOC_CONTACT.search(first)
    if source:
        return f"让二狗整理我和{source[1]}的聊天记录 全部并写入云盘"
    # Keep explicit author-supplied titles and body intact.
    direct = re.fullmatch(r"(?:请|帮我)?(?:让二狗)?(?:写|创建|新建)(?:一篇|一份)?飞书文档[：: ]*(.*)",first)
    named = re.search(r"(?:标题|命名|名称)(?:是|为|叫|用|[：:])?\s*(?:《([^》]+)》|「([^」]+)」|“([^”]+)”|([^，,。；;]+))",first)
    title = (next(value for value in named.groups() if value) if named else
             direct[1] if direct else "整理报告")
    return f"让二狗写飞书文档 {title or '整理报告'}" + ("\n"+body if body.strip() else "")


def continuation(db, app, identity, message, lastrowid, publish, revision=None):
    # A quoted reply always takes precedence over the most recent conversation.
    key = message.get("parent_id") or message.get("root_id")
    legacy = []
    if not key:
        # Old versions sent revisions to ordinary chat. An identical immediate
        # retry can step over those copies, never over a different owner's turn.
        recent = db.execute("""select i.message,i.payload,r.event as verified,exists(
            select 1 from direct_team_reports d where d.app=i.app and d.event=i.event and d.finished=1)
            from inbox i
            left join owner_readwrite_requests r using(app,event)
            where i.app=? and i.rowid<? and i.rowid>? and json_extract(i.payload,'$.chat_type')='p2p'
            and json_extract(i.payload,'$.chat_id')=? order by i.rowid desc""",
            (app,lastrowid,identity.get("context_after_rowid",0),message.get("chat_id")))
        for candidate, raw, verified, dispatched in recent:
            if not verified:
                payload = json.loads(raw)
                if payload.get("message_type") == "text":
                    candidate_text = json.loads(payload["content"]).get("text","")
                    if isinstance(candidate_text,str):
                        if revision and candidate_text.strip() == revision.strip():
                            continue
                        if (dispatched and len(legacy)<3 and revision_request(candidate_text)
                                and not re.search(r"另一个|另一份|其他(?:人|项目)|换(?:个|成)|项目|让(?!二狗|他)",candidate_text)
                                and re.search(r"新.{0,5}消息|知识库|聊天记录",candidate_text)):
                            legacy.append(candidate_text)
                            cited = payload.get("parent_id") or payload.get("root_id")
                            if cited:
                                key = cited
                                break
                            continue
            key = candidate
            break
    previous = db.execute("""select i.payload,c.command,i.event from inbox i
        join owner_readwrite_requests r using(app,event)
        left join history_continuations c using(app,event)
        where i.app=? and r.owner=? and r.tenant=? and json_extract(i.payload,'$.chat_id')=?
        and (i.message=? or exists(select 1 from direct_finals f join outbox o on o.id=f.outgoing
            where f.app=i.app and f.event=i.event and o.remote=? and o.state='sent'))""",
        (app,identity["open_id"],identity["tenant_key"],message.get("chat_id"),key,key)).fetchone()
    if previous:
        from . import history
        prior = previous[1] or json.loads(json.loads(previous[0])["content"]).get("text","")
        review = history.REVIEW.fullmatch(history.canonical(prior))
        if review and review[1]:
            db.execute("insert or replace into knowledge_followups values(?,?,?)",(app,message["message_id"],previous[2]))
            if legacy and (review[1]!="二狗" or not prior.endswith((" 全部并写入云盘"," 全部并重整知识库"))
                    or any(m and m[1]!=review[3] for m in (DOC_CONTACT.search(v) for v in legacy))):
                return None
            if revision:
                same_source = re.search(r"消息|聊天|单聊|知识库|原文档|这份文档|那份文档",revision)
                if not same_source or review[1] != "二狗" or not re.search(r"写入云盘|重整知识库",prior):
                    return None
                explicit_contact = DOC_CONTACT.search(revision)
                if explicit_contact and explicit_contact[1] != review[3]:
                    return None
                if structured_revision(revision) or "重整知识库" in prior:
                    return f"让二狗整理我和{review[3]}的聊天记录 全部并重整知识库"
            if publish and "重整知识库" not in prior:
                return f"让{review[1]}整理我和{review[3]}的聊天记录 全部并写入云盘"
            return prior
    # A clarification or a progress query keeps the same task available for the
    # next DM; an unrelated turn ends this implicit context.
    linked = db.execute("""select c.command,k.parent_event from knowledge_intent_candidates c
        left join knowledge_followups k using(app,message)
        join inbox i using(app,event)
        where i.app=? and c.owner=? and c.tenant=? and json_extract(i.payload,'$.chat_id')=?
        and c.decision in ('status','clarify') and (i.message=? or exists(
        select 1 from direct_finals f join outbox o on o.id=f.outgoing
        where f.app=i.app and f.event=i.event and o.remote=? and o.state='sent'))""",
        (app,identity["open_id"],identity["tenant_key"],message.get("chat_id"),key,key)).fetchone()
    if linked and linked[1]:
        db.execute("insert or replace into knowledge_followups values(?,?,?)",(app,message["message_id"],linked[1]))
    return linked[0] if linked else None


def receive(db, identity, event):
    header = event['header']
    body = event['event']
    sender, message = body['sender'], body['message']
    if not (header.get('app_id') == identity['app_id']
            and header.get('tenant_key') == identity['tenant_key']
            and sender.get('sender_type') == 'user'
            and sender.get('sender_id', {}).get('open_id') == identity['open_id']
            and message.get('chat_type') in ('p2p','group')):
        return False
    topic = None
    if message['chat_type']=='group':
        topic=group_topic(db,identity,message)
        if not topic:
            return False
    with db:
        change = db.execute('insert or ignore into inbox(app,event,message,payload) values(?,?,?,?)',
                            (header['app_id'], header['event_id'], message['message_id'], json.dumps(message)))
        if change.rowcount:
            db.execute('insert into delivery_timing(app,event,received) values(?,?,?)',
                       (header['app_id'],header['event_id'],time.time()))
            if message['chat_type']=='p2p' and not identity.get('enquiry_role') and message.get('message_type')=='text':
                from . import document_access, documents, history, voice
                try:
                    text=json.loads(message['content']).get('text','')
                except (ValueError,AttributeError):
                    text=''
                linked=document_access.request(text) if isinstance(text,str) else None
                explicit_link=bool(document_access.urls(text)) if isinstance(text,str) else False
                if isinstance(text,str) and not linked and not explicit_link:
                    publish = FULL_PUBLICATION.fullmatch(text.strip())
                    revision = revision_request(text) and not publish
                    delivery = is_delivery_request(text) or revision
                    if delivery or history.SHORT_RETRY.fullmatch(text.strip()):
                        lookup = lambda publish: continuation(db,header["app_id"],identity,message,change.lastrowid,publish)
                        if revision:
                            resolved = continuation(db,header["app_id"],identity,message,change.lastrowid,True,revision=text)
                            named = DOC_CONTACT.search(text.partition("\n")[0])
                            if not resolved and named and re.search(r"知识库|飞书文档|云盘",text):
                                mode = "全部并重整知识库" if structured_revision(text) else "全部并写入云盘"
                                resolved = f"让二狗整理我和{named[1]}的聊天记录 {mode}"
                        elif publish:
                            resolved = lookup(True)
                        elif delivery:
                            resolved = document_assignment(text)
                            quoted = message.get("parent_id") or message.get("root_id")
                            refers_to_quote = re.search(r"(?:把|将)?(?:它|这些内容|这段(?:对话|聊天|记录|内容)|上面(?:的内容)?|这份摘要)", text)
                            use_quoted = quoted and refers_to_quote and not DOC_CONTACT.search(text) and "\n" not in text
                            if use_quoted:
                                resolved = lookup(True) or resolved
                        else:
                            resolved = lookup(False)
                        if resolved or delivery:
                            db.execute("insert into history_continuations values(?,?,?)",
                                       (header["app_id"],header["event_id"],resolved))
                        if resolved:
                            text = resolved
                if isinstance(text,str) and not (explicit_link or history.is_command(text) or documents.is_command(text) or voice.is_command(text)):
                    prior = continuation(db,header["app_id"],identity,message,change.lastrowid,False)
                    if prior and prior.endswith((" 全部并写入云盘"," 全部并重整知识库")):
                        db.execute("insert into knowledge_intent_candidates values(?,?,?,?,?,?,null)",
                                   (header["app_id"],header["event_id"],message["message_id"],identity["open_id"],identity["tenant_key"],prior))
                if isinstance(text,str) and (linked or history.is_command(text) or documents.is_command(text) or voice.is_command(text)):
                    db.execute('insert into owner_readwrite_requests values(?,?,?,?,?)',
                               (header['app_id'],header['event_id'],message['message_id'],identity['open_id'],identity['tenant_key']))
        if change.rowcount and topic:
            db.execute('insert into group_enquiries values(?,?,?,?,?)',
                       (header['app_id'],header['event_id'],topic['task'],message['chat_id'],topic['remote']))
        if change.rowcount and identity.get('enquiry_role'):
            db.execute('insert into owner_enquiries values(?,?,?,?,?)',
                       (header['app_id'],header['event_id'],message['message_id'],identity['enquiry_role'],identity['open_id']))
    return bool(change.rowcount)


def listen(channel,identity,wake):
    import asyncio
    import multiprocessing
    import os
    import threading
    from .routing import journal
    parent = multiprocessing.parent_process()
    if parent is not None:
        def stop_with_gateway():
            from multiprocessing.connection import wait
            wait([parent.sentinel])
            os._exit(0)
        threading.Thread(target=stop_with_gateway,daemon=True).start()
    asyncio.set_event_loop(asyncio.new_event_loop())
    import lark_oapi as lark
    if identity.get('enquiry_role') and not identity.get('bot_open_id'):
        from .publisher import Publisher,API,checked
        import logging
        db=journal(channel)
        pub=Publisher({'steward':identity},db,channel.parent)
        try:
            # Resolve the identity whose mentions should count, per app and tenant.
            data=checked(pub.http.get(API+'/bot/v3/info',headers=pub.headers(identity),timeout=8))
            identity={**identity,'bot_open_id':data['bot']['open_id']}
        except Exception as error:
            logging.warning('Cannot identify specialist for indirect mentions (%s); direct topic replies remain available',type(error).__name__)
        finally:
            pub.http.close()
            db.close()
    def incoming(event):
        db = journal(channel)
        try:
            if receive(db,identity,json.loads(lark.JSON.marshal(event))):
                wake.set()
        finally:
            db.close()

    handler = lark.EventDispatcherHandler.builder('','').register_p2_im_message_receive_v1(incoming).build()
    client = lark.ws.Client(identity['app_id'],os.environ[identity['secret_env']],event_handler=handler)
    client.start()
