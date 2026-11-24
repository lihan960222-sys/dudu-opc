import json
from contextlib import closing
from types import SimpleNamespace

import httpx
import pytest

from app.feishu import documents
from app.feishu.ingress import receive
from app.feishu.routing import journal
from app.feishu.worker import Worker


@pytest.fixture
def setup_case(tmp_path, monkeypatch):
    identity = {'app_id': 'app', 'tenant_key': 'tenant', 'open_id': 'owner'}
    cfg = {'steward': identity, 'roles': {}, 'document_target': {
        'folder_token': 'folder1', 'tenant_origin': 'https://demo.feishu.cn'}}
    db = journal(tmp_path / 'gateway.sqlite')
    requests = []
    def respond(req):
        requests.append(req)
        if len(requests) == 1:
            data = {'document': {'document_id': 'doc123', 'revision_id': 1}}
        else:
            data = {'children': json.loads(req.content)['children']}
        return httpx.Response(200, json={'code': 0, 'data': data})
    pub = SimpleNamespace(db=db, http=httpx.Client(transport=httpx.MockTransport(respond)))
    worker = Worker(cfg, pub)
    def user_headers(config, *, required):
        assert config is cfg and required == {'docx:document:create', 'docx:document:write_only'}
        return {'Authorization': 'Bearer fake-user'}
    monkeypatch.setattr(documents, 'user_headers', user_headers)
    def request(text='写飞书文档 周报\n## 进展\n完成验收', **options):
        msg = dict(chat_type='p2p', chat_id='chat1', message_type='text', message_id='om_question',
                   content=json.dumps({'text': text}), **options)
        event = {'header': {'app_id': 'app', 'event_id': 'event', 'tenant_key': 'tenant'},
                 'event': {'message': msg, 'sender': {'sender_type': 'user', 'sender_id': {'open_id': 'owner'}}}}
        assert receive(db, identity, event)
        assert not receive(db, identity, event)
        return db.execute("select * from inbox where event='event'").fetchone()
    yield worker, request, requests
    worker.api.close()
    pub.http.close()
    db.close()


def reply(db):
    return json.loads(db.execute("select body from outbox where state='pending'").fetchone()[0])


@pytest.mark.asyncio
async def test_owner_dm_creates_sectioned_document_once(setup_case):
    worker, request, calls = setup_case
    row = request()
    await worker.process(row)
    await worker.process(row)
    assert len(calls) == 2
    create, write = calls
    assert create.url.path == '/open-apis/docx/v1/documents'
    assert json.loads(create.content) == {'folder_token': 'folder1', 'title': '周报'}
    assert write.url.path.endswith('/documents/doc123/blocks/doc123/children')
    assert write.url.params['document_revision_id'] == '1' and write.url.params['client_token']
    assert all(r.method == 'POST' and r.headers['Authorization'] == 'Bearer fake-user' for r in calls)
    assert [b['block_type'] for b in json.loads(write.content)['children']] == [4, 2]
    assert reply(worker.db)['reply_to'] == 'om_question'
    assert 'https://demo.feishu.cn/docx/doc123' in reply(worker.db)['text']
    assert worker.db.execute('select count(*) from outbox').fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['create', 'write'])
async def test_crash_around_remote_change_stops_republication(setup_case, stage, tmp_path):
    worker, request, calls = setup_case
    row = request()
    old = worker.pub.http.post
    def interrupted(*args, **kwargs):
        result = old(*args, **kwargs)
        if len(calls) == (1 if stage == 'create' else 2):
            raise KeyboardInterrupt()
        return result
    worker.pub.http.post = interrupted
    with pytest.raises(KeyboardInterrupt):
        await worker.process(row)
    with closing(journal(tmp_path / 'gateway.sqlite')) as recovered_db:
        reopened = Worker(worker.cfg, SimpleNamespace(db=recovered_db, http=worker.pub.http))
        recovered = recovered_db.execute('select * from inbox').fetchone()
        await reopened.process(recovered)
        await reopened.process(recovered)
        assert len(calls) == (1 if stage == 'create' else 2)
        assert '核对' in reply(recovered_db)['text']
        assert ('https://demo.feishu.cn/docx/doc123' in reply(recovered_db)['text']) == (stage == 'write')
        reopened.api.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('issue', ['missing_target', 'invalid_target', 'missing_proof', 'unknown_reply', 'wrong_chat', 'expired_auth'])
async def test_no_remote_write_on_unverified_source_or_destination(setup_case, issue, monkeypatch):
    worker, request, calls = setup_case
    row = request('写飞书文档 周报' if issue in ('unknown_reply','wrong_chat') else '写飞书文档 周报\n正文',
                  parent_id='om_old')
    if issue == 'missing_target':
        worker.cfg.pop('document_target')
    if issue == 'invalid_target':
        worker.cfg['document_target']['tenant_origin'] = 'https://fake.feishu.cn.invalid'
    if issue == 'missing_proof':
        worker.db.execute('delete from owner_readwrite_requests')
    if issue == 'wrong_chat':
        seed_answer(worker.db,'other')
    if issue == 'expired_auth':
        def denied(*args, **kwargs):
            raise ValueError('private details')
        monkeypatch.setattr(documents, 'user_headers', denied)
    await worker.process(row)
    assert not calls
    if issue != 'missing_proof':
        assert 'private details' not in reply(worker.db)['text']


def seed_answer(db, chat):
    msg = json.dumps({'chat_id': chat, 'chat_type': 'p2p'})
    with db:
        db.execute("insert into inbox values('app','old','om_orig',?,'done',null,null)", (msg,))
        db.execute("insert into direct_turns values('app','old',1,2)")
        db.execute("insert into direct_finals values('app','old','out')")
        db.execute("insert into outbox(id,role,body,state,remote) values('out','steward',?,'sent','om_old')",
                   (json.dumps({'text':'上次的回答','reply_to':'om_orig'}),))


@pytest.mark.asyncio
async def test_replied_completed_answer_only(setup_case):
    worker, request, calls = setup_case
    seed_answer(worker.db,'chat1')
    await worker.process(request('写飞书文档 周报', parent_id='om_old'))
    assert json.loads(calls[1].content)['children'][0]['text']['elements'][0]['text_run']['content']=='上次的回答'
@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['create','write'])
async def test_permission_errors_are_private_and_not_replayed(setup_case, stage):
    worker, request, calls = setup_case
    row = request()
    old = worker.pub.http.post
    def denied(*args, **kwargs):
        response = old(*args, **kwargs)
        if len(calls) == (1 if stage == 'create' else 2):
            return httpx.Response(403, request=response.request, json={'code':1770040,'msg':'private diagnostic'})
        return response
    worker.pub.http.post = denied
    await worker.process(row)
    await worker.process(row)
    feedback = reply(worker.db)['text']
    assert '目标文件夹' in feedback and 'private diagnostic' not in feedback
    assert ('https://demo.feishu.cn/docx/doc123' in feedback) == (stage == 'write')
    assert len(calls) == (1 if stage == 'create' else 2)


def test_scope_and_section_limits():
    assert not documents.is_command('解释一下怎么写飞书文档')
    assert not documents.is_command('写飞书文档教程')
    assert documents.is_command('请帮我创建一份飞书文档：会议纪要')
    with pytest.raises(ValueError):
        documents.blocks('x\n' * 51)
    with pytest.raises(ValueError):
        documents.blocks('x' * 24001)
    assert len(documents.blocks('字' * 1201)) == 2

@pytest.mark.parametrize('field,value', [('chat_type','group'),('open_id','stranger'),('tenant_key','other')])
def test_foreign_sender_tenant_and_group_never_authorize_a_write(tmp_path, field, value):
    identity = {'app_id':'app','tenant_key':'tenant','open_id':'owner'}
    msg = {'chat_type':'p2p','chat_id':'chat','message_type':'text','message_id':'om1',
           'content':json.dumps({'text':'写飞书文档 周报\n正文'})}
    header = {'app_id':'app','tenant_key':'tenant','event_id':'e'}
    sender = {'sender_type':'user','sender_id':{'open_id':'owner'}}
    if field == 'chat_type':
        msg[field] = value
    elif field == 'open_id':
        sender['sender_id'][field] = value
    else:
        header[field] = value
    with closing(journal(tmp_path/'db')) as db:
        assert not receive(db, identity, {'header':header,'event':{'message':msg,'sender':sender}})
        assert not db.execute('select 1 from owner_readwrite_requests').fetchone()


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['approved', 'removed_contact', 'changed_employee', 'oauth_revoked',
                                  'cloud_disabled', 'other_room', 'local_digest', 'revoked_after_create'])
async def test_explicit_private_summary_export_validates_origin_and_current_consent(setup_case, case, monkeypatch):
    from app.feishu import history

    worker, request, calls = setup_case
    source = {'chat_id':'oc_peer', 'peer_open_id':'ou_peer', 'agent_id':12}
    worker.cfg.update(history_sources={'小周':source}, allow_codex_summary=case!='cloud_disabled')
    chapter = '项目负责人应尽快确认采购计划。'
    evidence = '机密材料只给收件人看'
    db = worker.db
    chat = 'chat2' if case=='other_room' else 'chat1'
    seed_answer(db, chat)
    with db:
        db.execute("update outbox set body=? where id='out'",
                   (json.dumps({'reply_to':'om_orig', 'text':chapter+'\n  依据：'+evidence}),))
        db.execute("insert into owner_readwrite_requests values('app','old','om_orig','owner','tenant')")
        db.execute("""insert into private_history_jobs(app,event,alias,chat,peer,assignee,state)
                   values('app','old','小周','oc_peer','ou_peer',12,?)""",
                   ('local_completed' if case=='local_digest' else 'codex_completed',))
        db.execute("insert into history_reply_policy values('out','小周',?,'采购',12,1)", (json.dumps(source),))
        db.execute("insert into private_summary_notes values('app','old',?)", (chapter,))
    if case=='removed_contact':
        worker.cfg['history_sources'] = {}

    async def reviewer(cfg, selected, staff):
        assert selected==source and staff=='采购'
        return (13 if case=='changed_employee' else 12,)

    def authorized(cfg, scopes):
        assert scopes==history.SCOPES
        if case=='oauth_revoked' or (case=='revoked_after_create' and calls):
            raise ValueError('已断开')
        return {'Authorization':'Bearer test-owner'}

    monkeypatch.setattr(history, 'reviewer', reviewer)
    monkeypatch.setattr(history, 'headers', authorized)
    row = request('写飞书文档 项目摘要', parent_id='om_old')
    await worker.process(row)
    await worker.process(row)
    if case=='approved':
        assert len(calls)==2
        children = json.loads(calls[1].content)['children']
        assert [c['text']['elements'][0]['text_run']['content'] for c in children]==[chapter]
    else:
        assert len(calls)==(1 if case=='revoked_after_create' else 0)
    assert evidence not in reply(db)['text']
