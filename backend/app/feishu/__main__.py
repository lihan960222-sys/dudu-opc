"""Opt-in Feishu gateway. Run alongside the local backend under the same profile."""
import argparse
import asyncio
import json
import logging
import os
import threading
import multiprocessing
from pathlib import Path

from .ingress import listen
from .publisher import Publisher
from .routing import bind_config, enqueue, journal
from .worker import Worker


async def verify_chat(cfg,thread_id):
    from app.db import session
    from app.db.models import Thread
    from app.team.service import team_for
    async with session.AsyncSessionLocal() as db:
        team=await team_for(db,cfg['owner_id'])
        thread=await db.get(Thread,thread_id)
        if not (thread and team and thread.owner_id==cfg['owner_id'] and thread.kind=='chat'
                and thread.archived_at is None and thread.job_id is None and thread.room_id is None
                and thread.agent_id==team.lead_agent_id==cfg['steward']['agent_id']):
            raise ValueError('请选择当前老板与已配置的团队管家的普通会话，且该会话仍在使用。')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config',default=os.getenv('FEISHU_CONFIG'))
    parser.add_argument('--bind-steward-thread',type=int,metavar='ID',help='Bind an existing ordinary chat with the current team lead')
    parser.add_argument('--timings',action='store_true',help='Show measured queue, turn and delivery intervals for recent events')
    parser.add_argument('--status',action='store_true',help='List events and messages needing attention')
    parser.add_argument('--confirm-message',nargs=2,metavar=('OUTBOX_ID','REMOTE_ID'),help='Record a verified remote delivery')
    parser.add_argument('--retry-unsent',metavar='OUTBOX_ID',help='Retry after checking that no remote message exists')
    parser.add_argument('--resume-held',metavar='EVENT',help='Retry a held event after resolving its recorded error')
    parser.add_argument('--artifact',help='Task-relative artifact path')
    parser.add_argument('--task',type=int)
    parser.add_argument('--audience',choices=('private','group'),default='private')
    parser.add_argument('--kind',choices=('image','audio','media','file'),default='file')
    args = parser.parse_args()
    if not args.config:
        parser.error('Set FEISHU_CONFIG or pass --config')
    path = Path(args.config).resolve(strict=True)
    cfg = json.loads(path.read_text(encoding='utf-8-sig'))
    if cfg.get('owner_id') != 'local-owner':
        parser.error('独立本机后端使用 local-owner；网关与后端须连接同一数据库。')
    if args.bind_steward_thread is not None:
        from .setup_specialist import write_atomic
        try:
            asyncio.run(verify_chat(cfg,args.bind_steward_thread))
        except ValueError as error:
            parser.error(str(error))
        cfg['steward_thread']=args.bind_steward_thread
        write_atomic(path,(json.dumps(cfg,ensure_ascii=False,indent=2)+'\n').encode('utf-8'))
        print('已绑定管家会话。网关下次启动时使用此配置。')
        return
    directory = (path.parent / cfg['state_dir']).resolve()
    directory.mkdir(parents=True,exist_ok=True)
    channel = directory / 'channel.sqlite3'
    identity = cfg['steward']
    with journal(channel) as initial:
        bind_config(initial,cfg)
    initial.close()
    if args.timings:
        from .timing import report
        with journal(channel) as db:
            for row in report(db):
                print(json.dumps(dict(row)))
        return
    if args.status or args.confirm_message or args.retry_unsent:
        with journal(channel) as db:
            if args.status:
                for sql in ("select event,task,state,error from inbox where state not in ('done','resolved')",
                            "select id,task,role,state,error from outbox where state!='sent'"):
                    for row in db.execute(sql):
                        print(json.dumps(dict(row),ensure_ascii=False))
            elif args.confirm_message:
                key, remote = args.confirm_message
                if not remote.startswith('om_'):
                    parser.error('Expected a Feishu message ID')
                if not db.execute("update outbox set remote=?,state='sent',error=null where id=? and state='verify'",(remote,key)).rowcount:
                    parser.error('Unknown unconfirmed delivery')
            elif not db.execute("update outbox set state='pending',error=null where id=? and state='verify'",(args.retry_unsent,)).rowcount:
                parser.error('Unknown unconfirmed delivery')
        return
    if args.resume_held:
        with journal(channel) as db:
            updated = db.execute("update inbox set state=case when task is null then 'received' else 'bound' end,error=null where app=? and event=? and state='held'",(identity['app_id'],args.resume_held))
            if not updated.rowcount:
                parser.error('Unknown held event')
        return
    if args.artifact:
        with journal(channel) as db:
            row = db.execute('select role from bindings where task=?',(args.task,)).fetchone()
            if not row:
                parser.error('Unknown task')
            enqueue(db,args.task,'steward' if args.audience=='private' else row[0],
                    {'kind':args.kind,'file':args.artifact})
        return

    if not cfg.get('steward_thread'):
        parser.error('请先运行 --bind-steward-thread 会话编号，绑定团队管家的普通会话。')
    cfg['_access_config_path'] = str(path)
    try:
        asyncio.run(verify_chat(cfg,cfg['steward_thread']))
    except ValueError as error:
        parser.error(str(error))
    # Keep the SDK event loop on the main thread. A separate worker owns its
    # database connection; the socket callback only commits the incoming event.
    lock = (directory / 'gateway.lock').open('a+b')
    if os.name == 'nt':
        import msvcrt
        lock.seek(0)
        msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    else:
        import fcntl
        fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
    from .setup_specialist import load_secrets
    load_secrets(path,cfg)
    context = multiprocessing.get_context('spawn')
    wake = context.Event()
    specialists = [{**role,'tenant_key':identity['tenant_key'],'open_id':role['owner_open_id'],'enquiry_role':name}
                   for name,role in cfg['roles'].items() if role.get('owner_open_id')]
    children = {}
    stop = threading.Event()

    def supervise():
        while not stop.is_set():
            for specialist in specialists:
                name = specialist['enquiry_role']
                child = children.get(name)
                if child and child.is_alive():
                    continue
                if child:
                    child.join()
                    logging.warning('Reconnecting specialist listener %s (previous exit %s)',name,child.exitcode)
                    child.close()
                child = context.Process(target=listen,args=(channel,specialist,wake),daemon=True)
                child.start()
                children[name] = child
            stop.wait(30)

    monitor = threading.Thread(target=supervise,daemon=True)
    monitor.start()

    def work():
        db = journal(channel)
        try:
            asyncio.run(Worker(cfg,Publisher(cfg,db,directory)).run(wake))
        except BaseException:
            import logging
            logging.exception('Feishu worker stopped')
            os._exit(1)

    thread = threading.Thread(target=work,daemon=True)
    thread.start()
    try:
        listen(channel,identity,wake)
    finally:
        stop.set()
        monitor.join()
        for child in children.values():
            child.terminate()
            child.join(timeout=5)


if __name__ == '__main__':
    main()
