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
    create table if not exists delivery_timing(
        app text,event text,received real,submit real,accepted real,engine_start real,engine_end real,
        outgoing text unique,sent real,primary key(app,event));
    create table if not exists direct_targets(app text,event text,thread integer,primary key(app,event));
    create table if not exists direct_team_reports(
        app text,event text,job integer unique,question text,finished integer default 0,checked integer default 0,
        primary key(app,event));
    create table if not exists direct_hiring_notices(
        app text,event text,request text,primary key(app,event,request));
    create table if not exists owner_readwrite_requests(
        app text,event text,message text,owner text,tenant text,primary key(app,event));
    create table if not exists private_voice(
        outgoing text primary key,app text,event text,duration integer,unique(app,event));
    create table if not exists private_history_jobs(
        id integer primary key,app text,event text,alias text,chat text,peer text,assignee integer,
        start integer,end integer,cap integer,state text default 'assigned',matched integer,
        unique(app,event));
    create table if not exists history_reply_policy(
        outgoing text primary key,alias text,source text,staff text,actor integer,cloud integer);
    create table if not exists private_summary_notes(
        app text,event text,notes text,primary key(app,event));
    create table if not exists group_enquiries(
        app text,event text,task integer,chat text,topic text,primary key(app,event));
    create table if not exists owner_enquiries(
        app text,event text,message text,role text,owner_open_id text,primary key(app,event));
    create table if not exists enquiry_replies(
        outgoing text primary key,app text,event text,role text,unique(app,event));
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


def ensure_group_root(db, task, role, title):
    if not db.execute('select 1 from outbox where task=? and role=? and is_root=1',(task,role)).fetchone():
        enqueue(db,task,role,{'text':f'任务 #{task} · {title}'},root=True)


def bind_config(db, cfg):
    """Bind persisted destinations to a tenant and owner before reading the queue."""
    steward = cfg['steward']
    roles = cfg['roles']
    apps = [steward['app_id']] + [r['app_id'] for r in roles.values()]
    agents = [steward['agent_id']] + [r['agent_id'] for r in roles.values()]
    if ('steward' in roles or len(set(apps)) != len(apps)
            or len(set(agents)) != len(agents)):
        raise ValueError('每个角色需要独立的应用身份和本地角色 ID')
    ids = {'owner':[cfg['owner_id'],cfg['project'],steward['tenant_key'],steward['open_id'],apps[0],agents[0]]}
    ids.update({'role:'+name:[r['app_id'],r['agent_id'],r['chat_id']] for name,r in roles.items()})
    for name, role in roles.items():
        if role.get('owner_open_id'):
            if not role['owner_open_id'].startswith('ou_'):
                raise ValueError('专业机器人必须配置该应用下老板的 open_id')
            ids['enquiry:'+name] = [role['app_id'],steward['tenant_key'],role['owner_open_id']]
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
