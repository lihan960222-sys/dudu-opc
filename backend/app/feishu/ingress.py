"""Accept authenticated owner messages for a single application and destination."""
import json
import time


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
                from . import documents, history
                try:
                    text=json.loads(message['content']).get('text','')
                except (ValueError,AttributeError):
                    text=''
                if isinstance(text,str) and (history.is_command(text) or documents.is_command(text)):
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
