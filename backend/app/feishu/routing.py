"""A private SQLite journal for channel delivery, separate from the task board."""
import json
import sqlite3
from uuid import uuid4


def journal(path):
    db = sqlite3.connect(path, timeout=20)
    db.row_factory = sqlite3.Row
    db.executescript("""
    pragma journal_mode=WAL;
    create table if not exists inbox(
        app text, event text, message text, payload text, state text default 'received',
        task integer, error text, primary key(app,event), unique(app,message));
    create table if not exists outbox(
        id text primary key, task integer, role text, body text, is_root integer,
        state text default 'pending', remote text, error text, created real default (unixepoch()));
    create table if not exists resources(
        app text, message text, key text, task integer, name text, path text, digest text,
        primary key(app,message,key));
    create table if not exists uploads(
        app text, digest text, kind text, name text, key text, primary key(app,digest,kind,name));
    """)
    return db


def parts(kind, content):
    if kind == 'post':
        if 'content' not in content:
            content = content.get('zh_cn') or next(iter(content.values()), {})
        result = parts('text', {'text':content['title']}) if content.get('title') else []
        for row in content.get('content', []):
            for node in row:
                result.extend(parts({'img':'image'}.get(node['tag'], node['tag']), node))
        return result
    if kind in ('text','a','at'):
        return [{'kind':'text','text':content.get('text') or content.get('user_name','')}]
    if kind in ('image','audio','file','media'):
        key = content.get('image_key') if kind == 'image' else content.get('file_key')
        if key:
            return [{'kind':kind,'key':key,'name':content.get('file_name') or kind}]
    return [{'kind':'text','text':f'尚未支持的消息类型（{kind}）'}]


def enqueue(db, task, role, body, *, root=False):
    outgoing = str(uuid4())
    db.execute('insert into outbox(id,task,role,body,is_root) values(?,?,?,?,?)',
               (outgoing, task, role, json.dumps(body, ensure_ascii=False), int(root)))
    return outgoing


def bind_config(db, cfg):
    """Bind persisted destinations to a tenant and owner before reading the queue."""
    steward = cfg['steward']
    roles = cfg['roles']
    apps = [steward['app_id']] + [r['app_id'] for r in roles.values()]
    agents = [steward['agent_id']] + [r['agent_id'] for r in roles.values()]
    if ('steward' in roles or not roles or len(set(apps)) != len(apps)
            or len(set(agents)) != len(agents)):
        raise ValueError('每个角色需要独立的应用身份和本地角色 ID')
    ids = {'owner':[cfg['owner_id'],cfg['project'],steward['tenant_key'],steward['open_id'],apps[0],agents[0]]}
    ids.update({'role:'+name:[r['app_id'],r['agent_id'],r['chat_id']] for name,r in roles.items()})
    with db:
        db.execute('create table if not exists identities(name text primary key, binding text)')
        for key,value in ids.items():
            raw = json.dumps(value)
            db.execute('insert or ignore into identities values(?,?)',(key,raw))
            if db.execute('select binding from identities where name=?',(key,)).fetchone()[0] != raw:
                raise ValueError('收件范围或身份已变更，请配置单独的 state_dir')

def task_requirements(text):
    """Optional explicit fields, carried through to the planner and the model."""
    import re
    fields = {}
    for line in text.splitlines():
        match = re.fullmatch(r"\s*(版本|验收)\s*[:：]\s*(.{1,1000})\s*", line)
        if match:
            key = "version" if match[1] == "版本" else "acceptance"
            fields[key] = match[2].strip()
    return fields
