import json
from app.feishu.ingress import receive
from app.feishu.routing import journal, parts


def test_only_owner_dm_and_redelivery(tmp_path):
    db = journal(tmp_path / 'journal.db')
    cfg = {'app_id': 'a', 'open_id': 'user', 'tenant_key': 't'}
    event = {'header': {'app_id':'a','tenant_key':'t','event_id':'e'}, 'event': {'sender': {'sender_type':'user', 'sender_id':{'open_id':'user'}}, 'message': {'message_id':'m','chat_type':'group','message_type':'text','content':json.dumps({'text':'hello'})}}}
    assert not receive(db, cfg, event)
    event['event']['message']['chat_type'] = 'p2p'
    assert receive(db, cfg, event)
    assert not receive(db, cfg, event)
    assert db.execute('select count(*) from inbox').fetchone()[0] == 1


def test_mixed_post_order():
    data = {'content':[[{'tag':'text','text':'before'}, {'tag':'img','image_key':'im'}, {'tag':'text','text':'after'}]]}
    assert [p['kind'] for p in parts('post',data)] == ['text','image','text']

def test_identity_scoped_delivery_and_media(tmp_path):
    import httpx
    from app.feishu.publisher import Publisher
    from app.feishu.routing import enqueue
    db = journal(tmp_path/'state.sqlite')
    cfg = {'artifact_root':str(tmp_path),'steward':{'app_id':'st','open_id':'owner'},
           'roles':{'weld':{'app_id':'w','chat_id':'room'}}}
    pub = Publisher(cfg,db,tmp_path)
    pub.headers = lambda ident: {'Authorization':ident['app_id']}
    received = []
    def handle(request):
        received.append(request)
        if request.url.path.endswith('/files'):
            return httpx.Response(200,json={'code':0,'data':{'file_key':'key'}})
        if '/resources/' in request.url.path:
            return httpx.Response(200,content=b'resource')
        return httpx.Response(200,json={'code':0,'data':{'message_id':'sent'}})
    pub.http = httpx.Client(transport=httpx.MockTransport(handle))
    artifact = tmp_path/'42'/'log.txt'
    artifact.parent.mkdir()
    artifact.write_text('Example')
    for audience in ('weld','steward'):
        with db:
            key = enqueue(db,42,audience,{'kind':'file','file':'log.txt'},root=True)
        row = db.execute('select * from outbox where id=?',(key,)).fetchone()
        assert pub.deliver(row) == 'sent'
        req = received[-1]
        assert json.loads(req.content)['receive_id'] == ('owner' if audience=='steward' else 'room')
        assert req.headers['Authorization'] == cfg.get(audience,cfg['roles']['weld'])['app_id']
        db.execute("update outbox set state='sent',remote='sent' where id=?",(key,))
    assert db.execute('select count(*) from uploads').fetchone()[0] == 2
    key = enqueue(db,42,'weld',{'text':'progress'})
    pub.deliver(db.execute('select * from outbox where id=?',(key,)).fetchone())
    assert received[-1].url.path.endswith('/sent/reply')
    a = pub.download(42,'m1',{'kind':'file','key':'key','name':'log.txt'})
    b = pub.download(43,'m2',{'kind':'file','key':'key','name':'log.txt'})
    assert a != b and a.read_bytes() == b.read_bytes() == b'resource'


def test_queue_cannot_change_destination(tmp_path):
    import pytest
    from app.feishu.routing import bind_config
    db = journal(tmp_path/'state.sqlite')
    cfg = {'owner_id':'u','project':'p','steward':{'agent_id':1,'app_id':'a','open_id':'ou','tenant_key':'tenant'},
           'roles':{'weld':{'agent_id':2,'app_id':'b','chat_id':'work'}}}
    bind_config(db,cfg)
    cfg['roles']['weld']['chat_id'] = 'other'
    with pytest.raises(ValueError):
        bind_config(db,cfg)

def test_steward_cannot_select_other_role_task():
    from app.feishu.steward import validate_choice
    roles = ['焊接','视觉']
    tasks = [{'task':7,'title':'局部应变','role':'焊接'}]
    choice = {'intent':'append','role':'焊接','task':7,'title':'应变'}
    assert validate_choice(choice,roles,tasks) == ('append','焊接','局部应变',7)
    assert validate_choice({**choice,'role':'视觉'},roles,tasks) is None
    assert validate_choice({**choice,'intent':'independent'},roles,tasks) is None
    assert validate_choice({**choice,'task':None,'intent':'clarify'},roles,tasks) is None

def test_only_named_task_outputs_can_be_published(tmp_path):
    import pytest
    from app.feishu.artifacts import parse_report, snapshot
    folder = tmp_path/'role'/'任务'/'17'/'产物'/'飞书'
    folder.mkdir(parents=True)
    (folder/'result.txt').write_text('verified')
    summary, public, names = parse_report(json.dumps({'summary':'完整结论','group_update':'已检查单位','artifacts':['产物/飞书/result.txt']}))
    assert (summary,public) == ('完整结论','已检查单位')
    body = snapshot(tmp_path/'role',17,names[0],tmp_path/'journal')
    assert (tmp_path/'journal'/'outgoing'/'17'/body['snapshot']).read_text()=='verified'
    for path in ['../17/产物/飞书/result.txt','产物/飞书/../../private.txt','结果/log.txt']:
        with pytest.raises(ValueError):
            snapshot(tmp_path/'role',17,path,tmp_path/'journal')


def test_missing_root_leaves_child_pending(tmp_path):
    from app.feishu.worker import Worker
    from app.feishu.publisher import Publisher
    from app.feishu.routing import enqueue
    db = journal(tmp_path/'state.sqlite')
    worker = Worker({},Publisher({},db,tmp_path))
    key = enqueue(db,13,'weld',{'text':'progress'})
    worker.flush()
    assert db.execute('select state from outbox where id=?',(key,)).fetchone()[0]=='pending'

def test_routing_keeps_version_and_acceptance_separate():
    from app.feishu.routing import task_requirements
    assert task_requirements("新建 开发 批量导入\n版本：v2\n验收: 空目录可打开") == {
        "version": "v2", "acceptance": "空目录可打开",
    }
    assert task_requirements("追加一张截图") == {}
