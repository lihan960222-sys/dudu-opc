"""Publish an owner-selected text to a new document in one configured folder."""
import json
import re
from uuid import NAMESPACE_URL, uuid5

import httpx

from .publisher import API, checked
from .routing import enqueue
from .workspace_access import settings_for

COMMAND = re.compile(r'^(?:请(?:帮我)?|帮我)?(?:写|新建|创建)(?:一篇|一份)?飞书文档(?=$|[\s：:])')
NAMED = re.compile(r'^(?:请)?(?:让|叫)([\w·.\-]{1,40}?)(?=(?:写|新建|创建)(?:一篇|一份)?飞书文档)')
TOKEN = re.compile(r'[A-Za-z0-9]{1,128}')
SCOPES = {'docx:document:create', 'docx:document:write_only'}
GUIDE = '第一行写「写飞书文档 标题」，从第二行开始写正文。也可以直接回复嘟嘟已发出的回答，第一行写同样的指令。'
ACCESS = '请在本机确认飞书账号已授权创建和编辑文档，并检查目标文件夹是否存在、当前账号能否在里面新建文档。'


def user_headers(cfg, *, required):
    from .owner_oauth import user_headers as authorize
    return authorize(cfg, required=required)


def is_command(text):
    return bool(COMMAND.match(NAMED.sub('',text.strip(),count=1)))


def blocks(body):
    if not body.strip() or len(body) > 24000:
        raise ValueError('请提供 1 到 24000 个字符的正文。')
    children = []
    for line in body.splitlines():
        if not line.strip():
            continue
        heading = re.fullmatch(r'(#{1,3}) +(.+)', line)
        level = len(heading[1]) if heading else 0
        field = f'heading{level}' if heading else 'text'
        value = heading[2] if heading else line
        for start in range(0, len(value), 1200):
            run = {'text_run': {'content': value[start:start + 1200]}}
            children.append({'block_type': 2 + level, field: {'elements': [run]}})
    if len(children) > 50:
        raise ValueError('这篇文档超过了单次写入的 50 段上限。请减少段落或拆成两篇。')
    return children


def quoted_answer(db, row, msg):
    if not msg.get('parent_id') or not msg.get('chat_id'):
        return ''
    previous = db.execute("""select o.body,i.payload,i.message from direct_finals f
        join direct_turns d using(app,event) join inbox i using(app,event)
        join outbox o on o.id=f.outgoing where f.app=? and o.remote=? and o.state='sent'
        and o.role='steward' and o.task is null and i.state='done'
        and not exists(select 1 from owner_readwrite_requests x where x.app=f.app and x.event=f.event)
        and not exists(select 1 from history_reply_policy h where h.outgoing=o.id)""",
        (row['app'], msg['parent_id'])).fetchone()
    if previous:
        source, reply = json.loads(previous['payload']), json.loads(previous['body'])
        if (source.get('chat_type') == 'p2p' and source.get('chat_id') == msg['chat_id']
                and reply.get('reply_to') == previous['message']):
            return reply.get('text', '')
    return ''


def finish(worker, row, text):
    keys = row['app'], row['event']
    with worker.db:
        changed = worker.db.execute("update inbox set state='done' where app=? and event=? and state='received'", keys)
        if changed.rowcount:
            outgoing = enqueue(worker.db, None, 'steward', {'text': text, 'reply_to': row['message']})
            worker.db.execute('update delivery_timing set outgoing=? where app=? and event=?', (outgoing, *keys))


async def writer(cfg,name):
    from .history import reviewer
    from .owner_oauth import employee_config
    from .knowledge_document import target
    for role in cfg.get("roles",{}).values():
        employee=role["agent_id"]
        try:
            await reviewer(cfg,{"agent_id":employee},name)
        except ValueError:
            continue
        selected=employee_config(cfg,employee)
        if not selected.get("_employee_oauth"):
            raise ValueError("请指定已绑定飞书应用的员工。")
        folder,_=target(cfg,employee)
        return selected,employee,folder
    raise ValueError(f"请先在工作台为{name}绑定飞书应用。")


async def handle(worker, row, msg, text):
    if msg.get('message_type') != 'text' or not is_command(text):
        return False
    db = worker.db
    who = worker.cfg['steward']
    keys = row['app'], row['event']
    proof = db.execute("""select 1 from owner_readwrite_requests
        where app=? and event=? and message=? and owner=? and tenant=?""",
        (*keys, row['message'], who.get('open_id'), who.get('tenant_key'))).fetchone()
    if msg.get('chat_type') != 'p2p' or row['app'] != who['app_id'] or not proof:
        with db:
            db.execute("update inbox set state='ignored' where app=? and event=? and state='received'", keys)
        return True
    current = db.execute('select state from inbox where app=? and event=?', keys).fetchone()
    if not current or current[0] != 'received':
        return True
    db.execute("""create table if not exists document_publications(
        app text,event text,origin text not null,document text,primary key(app,event))""")
    previous = db.execute('select origin,document from document_publications where app=? and event=?', keys).fetchone()
    if previous:
        # The gateway owns an exclusive process lock. A live inbound row with a durable
        # reservation means its previous worker stopped before confirming publication.
        link = previous['origin'] + '/docx/' + previous['document'] if previous['document'] else ''
        finish(worker, row, '上次写入中断了。请先核对' + (f'这份文档的正文：{link}' if link else '目标文件夹里是否已有同名文档。'))
        return True
    named=NAMED.match(text.strip())
    name=named[1] if named else "二狗"
    try:
        cfg,employee,target=await writer(worker.cfg,name)
    except (ValueError,OSError,KeyError) as error:
        finish(worker,row,str(error))
        return True
    text=NAMED.sub("",text.strip(),count=1)
    if not target:
        finish(worker, row, '请先在工作台保存目标云盘文件夹。')
        return True
    folder, origin = target['folder_token'], target['tenant_origin']
    first, _, draft = text.strip().partition('\n')
    title = COMMAND.sub('', first, count=1).lstrip('：: ').strip()
    if not title or len(title) > 200:
        finish(worker, row, GUIDE + ' 标题最多 200 个字符。')
        return True
    try:
        headers = user_headers(cfg, required=SCOPES)
    except (ValueError, OSError, RuntimeError, httpx.HTTPError):
        finish(worker, row, ACCESS)
        return True
    body = draft if draft.strip() else quoted_answer(db, row, msg)
    private_summary = False
    if not body and msg.get('parent_id'):
        from .history import quoted_notes
        same_actor=db.execute(
            "select 1 from history_reply_policy h join outbox o on h.outgoing=o.id where o.remote=? and h.actor=?",
            (msg["parent_id"],employee)).fetchone()
        body = await quoted_notes(worker,row,msg) if same_actor else ''
        private_summary = bool(body)
    if not body:
        finish(worker, row, GUIDE + ' 如果这条回答是私聊摘录，或已无法核对授权，请手动粘贴你要写入的文字。')
        return True
    try:
        children = blocks(body)
    except ValueError as exc:
        finish(worker, row, str(exc))
        return True
    async def current_target():
        try:
            return (await writer(worker.cfg,name))[2]
        except (ValueError,OSError,KeyError):
            return {}
    if private_summary and await current_target() != target:
        finish(worker, row, '目标文件夹已变更，请确认后重新发起建档。')
        return True
    with db:
        reserved = db.execute('insert or ignore into document_publications values(?,?,?,null)', (*keys, origin))
    if not reserved.rowcount:
        return True
    link = ''
    try:
        created = checked(worker.pub.http.post(API + '/docx/v1/documents', headers=headers,
                          json={'folder_token': folder, 'title': title}, timeout=15))['data']['document']
        document = created['document_id']
        if not isinstance(document, str) or not TOKEN.fullmatch(document):
            raise ValueError('Invalid document ID')
        link = origin + '/docx/' + document
        with db:
            db.execute('update document_publications set document=? where app=? and event=?', (document, *keys))
        if private_summary:
            if (await current_target() != target
                    or await quoted_notes(worker, row, msg) != body):
                finish(worker, row, f'授权或目标文件夹已变更，摘要未写入。已创建的空文档：{link}')
                return True
            headers = user_headers(cfg, required=SCOPES)
        revision = created['revision_id']
        if type(revision) is not int or revision < 1:
            raise ValueError('Invalid document revision')
        url = API + f'/docx/v1/documents/{document}/blocks/{document}/children'
        params = {'document_revision_id': revision,
                  'client_token': str(uuid5(NAMESPACE_URL, f'feishu/docx/{who["app_id"]}/{row["message"]}/{document}'))}
        written = checked(worker.pub.http.post(url, headers=headers, params=params,
                          json={'children': children, 'index': 0}, timeout=15))
        if len(written['data']['children']) != len(children):
            raise ValueError('Unconfirmed document content')
    except (httpx.HTTPError, RuntimeError, OSError, ValueError, TypeError, KeyError):
        result = (f'文档已创建，正文写入结果待核对：{link}' if link else '建档结果待核对。请先到目标文件夹查看是否已有同名文档。')
        finish(worker, row, result + '\n' + ACCESS)
    else:
        finish(worker, row, f'已写好《{title}》。\n{link}')
    return True
