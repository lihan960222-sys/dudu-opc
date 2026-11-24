import json
from contextlib import closing
from types import SimpleNamespace

import httpx

from app.feishu.routing import journal
from app.feishu.worker import Worker


def test_direct_image_is_uploaded_to_steward_thread_with_selected_skills(tmp_path):
    path = tmp_path / "image"
    path.write_bytes(b"sample image bytes")
    requests = []

    def download(task, message, part):
        assert (task, message, part['key']) == (-3, 'om_input', 'img_key')
        return path

    def backend(request):
        requests.append(request.url.path)
        if request.url.path.endswith('/attachments'):
            assert path.read_bytes() in request.content
            return httpx.Response(201, json={'id':'att1'})
        body=json.loads(request.content)
        assert len(body.pop('direct_event_key'))==64
        assert body == {
            'prompt_text':'请查看附件并回复。','attachment_ids':['att1'],'skills':['humanizer-zh']}
        return httpx.Response(202, json={'id':9})

    with closing(journal(tmp_path / 'channel')) as db:
        def notice(row):
            assert json.loads(row['body'])['reply_to']=='om_input'
            requests.append('thinking')
            return 'om_thinking'
        pub = SimpleNamespace(db=db,download=download,deliver=notice)
        worker = Worker({'steward_thread':3,'steward_skills':['humanizer-zh']},pub)
        worker.api.close()
        with httpx.Client(base_url='http://localhost/',transport=httpx.MockTransport(backend)) as worker.api:
            row = {'app':'app','event':'ev','message':'om_input'}
            db.execute("insert into inbox(app,event,message) values(:app,:event,:message)",row)
            worker.direct_chat(row,{'message_type':'image','content':'{"image_key":"img_key"}'},'')
        assert requests == ['thinking','/threads/3/attachments','/threads/3/direct-turns']
        assert db.execute('select state from inbox').fetchone()[0] == 'direct_running'
