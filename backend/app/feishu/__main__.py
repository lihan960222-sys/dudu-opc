"""Opt-in Feishu gateway. Run alongside the local backend under the same profile."""
import argparse
import asyncio
import json
import os
import threading
from pathlib import Path

from .ingress import receive
from .publisher import Publisher
from .routing import bind_config, enqueue, journal
from .worker import Worker


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config',default=os.getenv('FEISHU_CONFIG'))
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
    directory = (path.parent / cfg['state_dir']).resolve()
    directory.mkdir(parents=True,exist_ok=True)
    channel = directory / 'channel.sqlite3'
    identity = cfg['steward']
    with journal(channel) as initial:
        bind_config(initial,cfg)
    initial.close()
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
    asyncio.set_event_loop(asyncio.new_event_loop())
    import lark_oapi as lark
    def incoming(event):
        db = journal(channel)
        try:
            receive(db,identity,json.loads(lark.JSON.marshal(event)))
        finally:
            db.close()

    def work():
        db = journal(channel)
        try:
            asyncio.run(Worker(cfg,Publisher(cfg,db,directory)).run())
        except BaseException:
            import logging
            logging.exception('Feishu worker stopped')
            os._exit(1)

    handler = lark.EventDispatcherHandler.builder('','').register_p2_im_message_receive_v1(incoming).build()
    thread = threading.Thread(target=work,daemon=True)
    thread.start()
    client = lark.ws.Client(identity['app_id'],os.environ[identity['secret_env']],event_handler=handler)
    client.start()


if __name__ == '__main__':
    main()
