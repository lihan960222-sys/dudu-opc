"""Accept only authorized human DMs on the steward app's authenticated socket."""
import json


def receive(db, identity, event):
    header = event['header']
    body = event['event']
    sender, message = body['sender'], body['message']
    if not (header.get('app_id') == identity['app_id']
            and header.get('tenant_key') == identity['tenant_key']
            and sender.get('sender_type') == 'user'
            and sender.get('sender_id', {}).get('open_id') == identity['open_id']
            and message.get('chat_type') == 'p2p'):
        return False
    with db:
        change = db.execute('insert or ignore into inbox(app,event,message,payload) values(?,?,?,?)',
                            (header['app_id'], header['event_id'], message['message_id'], json.dumps(message)))
    return bool(change.rowcount)
