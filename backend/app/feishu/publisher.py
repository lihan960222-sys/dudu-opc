"""Send only to destinations chosen in the local configuration."""
import hashlib
import json
import os
import time
from pathlib import Path
from urllib.parse import quote
from tempfile import TemporaryDirectory
from uuid import NAMESPACE_URL, uuid4, uuid5

from app.services import steward_reply

import httpx

API = 'https://open.feishu.cn/open-apis'
MAX_DOWNLOAD = 50 * 1024 * 1024


class PlatformError(RuntimeError):
    pass


def checked(response):
    response.raise_for_status()
    result = response.json()
    if result.get('code') != 0:
        raise PlatformError(f"Feishu error {result.get('code')}")
    return result


class Publisher:
    def __init__(self, config, db, directory):
        self.config, self.db, self.directory = config, db, Path(directory)
        self.tokens = {}
        self.http = httpx.Client(timeout=60)

    def headers(self, identity):
        app = identity['app_id']
        if app not in self.tokens or self.tokens[app][0] < time.time():
            result = checked(self.http.post(API + '/auth/v3/tenant_access_token/internal',
                json={'app_id':app, 'app_secret':os.environ[identity['secret_env']]}))
            self.tokens[app] = (time.time() + result['expire'] - 90, result['tenant_access_token'])
        return {'Authorization':'Bearer ' + self.tokens[app][1]}

    def download(self, task, message, part):
        identity = self.config['steward']
        args = (identity['app_id'], message, part['key'])
        row = self.db.execute('select * from resources where app=? and message=? and key=?', args).fetchone()
        if row:
            if row['task'] != task:
                raise ValueError('Attachment belongs to another task')
            return Path(row['path'])
        directory = self.directory / 'attachments' / str(int(task))
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / uuid4().hex
        url = API + '/im/v1/messages/' + quote(message,safe='') + '/resources/' + quote(part['key'],safe='')
        digest = hashlib.sha256()
        try:
            with self.http.stream('GET', url, headers=self.headers(identity),
                    params={'type':'image' if part['kind']=='image' else 'file'}) as response:
                response.raise_for_status()
                if 'application/json' in response.headers.get('content-type',''):
                    response.read()
                    checked(response)
                    raise PlatformError('Expected binary resource')
                with target.open('xb') as out:
                    for chunk in response.iter_bytes():
                        if out.tell() + len(chunk) > MAX_DOWNLOAD:
                            raise ValueError('文件超过本机接收上限 50 MiB')
                        out.write(chunk)
                        digest.update(chunk)
            with self.db:
                self.db.execute('insert into resources values(?,?,?,?,?,?,?)',
                                (*args, task, part['name'], str(target), digest.hexdigest()))
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return target

    def upload(self, identity, path, kind, *, duration=None):
        limit = (10 if kind == 'image' else 30)*1024*1024
        if path.stat().st_size > limit:
            raise ValueError(f'文件超过发送上限 {limit//1024//1024} MiB')
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream,'sha256').hexdigest()
            args = (identity['app_id'],digest,kind,path.name)
            prior = self.db.execute('select key from uploads where app=? and digest=? and kind=? and name=?', args).fetchone()
            if prior:
                return prior[0]
            stream.seek(0)
            image = kind == 'image'
            field = 'image' if image else 'file'
            fields = {'image_type':'message'} if image else {'file_type':{'audio':'opus','media':'mp4'}.get(kind,'stream'),'file_name':path.name}
            if duration is not None:
                fields['duration']=str(duration)
            result = checked(self.http.post(API + '/im/v1/' + field + 's',
                    headers=self.headers(identity),data=fields,files={field:(path.name,stream)}))
            key = result['data'][field+'_key']
        with self.db:
            self.db.execute('insert or replace into uploads values(?,?,?,?,?)', (*args,key))
        return key

    def recall_notice(self, remote):
        if not isinstance(remote,str) or not remote.startswith('om_'):
            raise ValueError('Invalid Feishu notice message ID')
        url = API + '/im/v1/messages/' + quote(remote,safe='')
        response = self.http.delete(url,headers=self.headers(self.config['steward']),timeout=10)
        # The platform also reports this state when an earlier recall succeeded
        # but its response was lost.
        if response.status_code==400 and response.json().get('code')==230110:
            return
        checked(response)

    def deliver(self, row):
        private = row['role'] == 'steward'
        role = self.config['steward'] if private else self.config['roles'][row['role']]
        body = json.loads(row['body'])
        kind = body.get('kind','text')
        if kind == 'text':
            content = {'text':body['text']}
        elif body.get('private_voice'):
            from .voice import resource
            path,ms=resource(self,row,body)
            content={'file_key':self.upload(role,path,kind,duration=ms)}
        else:
            snapshot = 'snapshot' in body
            root = self.directory/'outgoing' if snapshot else Path(self.config['artifact_root'])
            folder = root.resolve() / str(int(row['task']))
            path = (folder / body['snapshot' if snapshot else 'file']).resolve(strict=True)
            if folder.resolve(strict=True) != folder or not path.is_file() or not path.is_relative_to(folder) or kind not in ('image','audio','media','file'):
                raise ValueError('Invalid task artifact')
            content = {'image_key' if kind=='image' else 'file_key': self.upload(role,path,kind)}
        data = {'uuid':row['id'],'msg_type':kind,'content':json.dumps(content,ensure_ascii=False)}
        params = {}
        if body.get('enquiry_reply'):
            allowed = self.db.execute("""select q.message from enquiry_replies r join owner_enquiries q
                on (q.app,q.event,q.role)=(r.app,r.event,r.role)
                where r.outgoing=? and r.role=? and q.app=? and q.owner_open_id=?""",
                (row['id'],row['role'],role['app_id'],role.get('owner_open_id'))).fetchone()
            if private or kind!='text' or not allowed:
                raise ValueError('专业机器人私聊只能回复老板已验证的原消息')
            group=self.db.execute("""select g.chat,g.task from enquiry_replies r join group_enquiries g using(app,event)
                where r.outgoing=?""",(row['id'],)).fetchone()
            if group:
                if group['chat']!=role['chat_id'] or group['task']!=row['task']:
                    raise ValueError('工作群话题与当前任务不符')
                data['reply_in_thread']=True
            url = API + '/im/v1/messages/' + quote(allowed['message'],safe='') + '/reply'
        elif private and body.get('reply_to'):
            url = API + '/im/v1/messages/' + quote(body['reply_to'],safe='') + '/reply'
        elif not private and not role.get('chat_id'):
            params['receive_id_type'] = 'open_id'
            data['receive_id'] = role['owner_open_id']
            url = API + '/im/v1/messages'
        elif private or row['is_root']:
            params['receive_id_type'] = 'open_id' if private else 'chat_id'
            data['receive_id'] = role['open_id' if private else 'chat_id']
            url = API + '/im/v1/messages'
        else:
            root = self.db.execute("select remote from outbox where role=? and task=? and is_root=1 and state='sent'", (row['role'],row['task'])).fetchone()
            if not root:
                raise ValueError('工作群根消息尚未确认')
            url = API + '/im/v1/messages/' + quote(root[0],safe='') + '/reply'
            data['reply_in_thread'] = True
        if private and kind == "text":
            # Both messages use this row's verified destination and authorization.
            # Persist the attachment receipt before sending the summary; a retry
            # can resume after that point even if the original process exited.
            sent = self.db.execute("select remote from steward_reply_files where outgoing=?", (row["id"],)).fetchone()
            if len(body["text"]) > steward_reply.LIMIT and not sent:
                with TemporaryDirectory() as temp:
                    path = Path(temp) / "完整回复.md"
                    path.write_bytes(body["text"].encode("utf-8"))
                    file_key = self.upload(role, path, "file")
                attachment = {**data, "uuid": str(uuid5(NAMESPACE_URL, "steward-reply-file/" + row["id"])),
                              "msg_type": "file", "content": json.dumps({"file_key": file_key})}
                reply = checked(self.http.post(url, params=params, json=attachment, headers=self.headers(role)))
                with self.db:
                    self.db.execute("insert or replace into steward_reply_files values(?,?)",
                                    (row["id"], reply["data"]["message_id"]))
            data["content"] = json.dumps({"text":steward_reply.preview(body["text"], attached=bool(sent))}, ensure_ascii=False)
        result = checked(self.http.post(url,params=params,json=data,headers=self.headers(role)))
        return result['data']['message_id']
