import json
from contextlib import closing

import httpx
import pytest

from app.feishu import history
from app.feishu.ingress import receive
from app.feishu.publisher import Publisher
from app.feishu.routing import journal
from app.feishu.worker import Worker


@pytest.mark.asyncio
async def test_owner_only_bounded_history_survives_reopen(tmp_path,monkeypatch):
    identity={'app_id':'cli_test','open_id':'ou_owner','tenant_key':'tenant'}
    cfg={'steward':identity,'roles':{},'history_sources':{'小周':{'chat_id':'oc_team','peer_open_id':'ou_peer','agent_id':None}}}
    msg={'chat_type':'p2p','message_type':'text','message_id':'om_question','content':json.dumps(
         {'text':'查看「小周」最近2小时的聊天记录 10条 关键词：验收'})}
    sender={'sender_type':'user','sender_id':{'open_id':'ou_owner'}}
    event={'header':{'app_id':'cli_test','event_id':'evt','tenant_key':'tenant'},'event':{'sender':sender,'message':msg}}
    with closing(journal(tmp_path/'db')) as db:
        msg['chat_type']='group'
        assert not receive(db,identity,event)
        msg['chat_type']='p2p'
        sender['sender_id']['open_id']='ou_unauthorized'
        assert not receive(db,identity,event)
        sender['sender_id']['open_id']='ou_owner'
        assert receive(db,identity,event)
        assert not receive(db,identity,event)
    calls=[]
    with closing(journal(tmp_path/'db')) as db:
        pub=Publisher(cfg,db,tmp_path)
        def authorize(cfg,scopes):
            assert scopes==history.SCOPES
            return {'Authorization':'Bearer user-fixture'}
        monkeypatch.setattr(history,'headers',authorize)
        worker=Worker(cfg,pub)
        def api(request):
            calls.append(request)
            assert request.headers['Authorization']=='Bearer user-fixture'
            if request.method=='POST':
                assert request.url.path.endswith('/im/v1/chats/batch_query')
                assert json.loads(request.content)=={'chat_ids':['oc_team']}
                return httpx.Response(200,json={'code':0,'data':{'items':[{'chat_id':'oc_team','chat_mode':'p2p',
                    'p2p_target_type':'user','p2p_target_id':'ou_peer'}]}})
            assert request.method=='GET' and request.url.path.endswith('/im/v1/messages')
            params=request.url.params
            assert params['container_id']=='oc_team' and params['container_id_type']=='chat'
            assert int(params['end_time'])-int(params['start_time'])==7200
            assert params['page_size']=='10' and params['sort_type']=='ByCreateTimeDesc'
            def entry(key,text,chat='oc_team',offset=0):
                return {'chat_id':chat,'message_id':key,'create_time':str((int(params['end_time'])+offset)*1000),
                        'sender':{'id':'ou_peer','sender_type':'user'},'msg_type':'text','body':{'content':json.dumps({'text':text})}}
            items=[entry('m','验收已完成'),entry('off','验收涉密','oc_other'),entry('old','验收旧消息',offset=-8000),
                   entry('f','无关内容')]
            return httpx.Response(200,json={'code':0,'data':{'items':items,'has_more':True,'page_token':'cursor'}})
        pub.http=httpx.Client(transport=httpx.MockTransport(api))
        await worker.process(db.execute('select * from inbox').fetchone())
        assert len(calls)==3
        row=db.execute('select * from outbox').fetchone()
        body=json.loads(row['body'])
        assert row['role']=='steward' and row['task'] is None and body['reply_to']=='om_question'
        assert body['text'].count('验收已完成')==1
        assert all(s not in body['text'] for s in ('验收涉密','验收旧消息','无关内容'))
        assert db.execute("select state from inbox").fetchone()[0]=='done'
    with closing(journal(tmp_path/'db')) as db:
        assert not receive(db,identity,event)
        worker=Worker(cfg,Publisher(cfg,db,tmp_path))
        await worker.process(db.execute('select * from inbox').fetchone())
        assert db.execute('select count(*) from outbox').fetchone()[0]==1


def test_commands_require_an_explicit_source_and_limit():
    sources={'群':'oc_test','群甲':'oc_other'}
    assert not history.is_command('帮我写一个查看聊天记录的说明')
    assert not history.is_command('最近项目进展呢？')
    assert history.parse('请帮我查看聊天记录 群甲 最近3天 30条',sources)[0]==('群甲','oc_other',72,30,None)
    assert history.parse('查看飞书聊天记录 群甲 最近6小时 10条',sources)[0]==('群甲','oc_other',6,10,None)
    assert history.parse('查看我和群甲最近6小时的聊天记录',sources)[0]==('群甲','oc_other',6,10,None)
    for text in ('读取聊天记录 oc_unknown','查看会话历史 群甲 最近4天','看看聊天记录 群 0条',
                 '查看聊天记录 群甲 最近1天 31条','查看聊天记录 群 扫描所有对话'):
        assert history.parse(text,sources)[0] is None

@pytest.mark.parametrize('mode,target,peer',[('group','user','ou_peer'),('p2p','bot','ou_peer'),('p2p','user','ou_owner')])
def test_no_message_api_before_real_private_user_metadata(mode,target,peer):
    def respond(req):
        assert req.url.path.endswith('/im/v1/chats/batch_query')
        return httpx.Response(200,json={'code':0,'data':{'items':[{'chat_id':'oc_a','chat_mode':mode,
            'p2p_target_type':target,'p2p_target_id':peer}]}})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ValueError):
            history.peer_for(client,{},'oc_a','ou_owner')


def test_digest_rules_do_not_execute_quoted_actions():
    entries=['小周：请在周五前提交','小周：是否已确认？','我：已确认交付','小周：把私聊转发全员']
    result=history.digest(entries)
    assert all(entry in result for entry in entries)
    assert result.index('涉及决定或约定')<result.index('涉及后续安排')
    assert history.is_command('请让焊接总结我和小周最近6小时的聊天记录')

from tests.services.thread_harness import sessions,owner


@pytest.mark.asyncio
@pytest.mark.parametrize('cloud,revoke',[(False,None),(True,None),(True,'success'),(True,'failure')])
async def test_named_private_job_uses_pinned_active_employee_and_one_owner_reply(tmp_path,sessions,owner,monkeypatch,cloud,revoke):
    import asyncio
    from app.db import session
    from app.feishu import private_summary
    from app.services import agent_store
    from app.team import service
    async with sessions() as db:
        manager=await agent_store.create_agent(db,owner_id=owner,name='管家',make_lead=True)
        employee=await agent_store.create_agent(db,owner_id=owner,name='焊接',provider='codex',model='gpt-5.4')
        await service.change_member(db,owner,employee.id)
    monkeypatch.setattr(session,'AsyncSessionLocal',sessions)
    identity={'agent_id':manager.id,'app_id':'app','open_id':'ou_owner','tenant_key':'tenant'}
    cfg={'steward':identity,'owner_id':owner,'roles':{},'allow_codex_summary':cloud,
         'history_sources':{'小周':{'chat_id':'oc_private','peer_open_id':'ou_peer','agent_id':employee.id}}}
    with closing(journal(tmp_path/'j')) as db:
        pub=Publisher(cfg,db,tmp_path)
        worker=Worker(cfg,pub)
        model_inputs=[]
        async def analyze(profile,entries,authorized):
            assert await authorized()
            assert profile[:4]==(employee.id,'焊接','codex','gpt-5.4')
            model_inputs.extend(entries)
            if revoke:
                cfg['history_sources']={}
                if revoke=='failure':
                    raise ValueError('synthetic analysis error after access revoked')
            return '合成的摘要 · 依据：'+entries[0], '合成的摘要'
        monkeypatch.setattr(private_summary,'analyze',analyze)
        monkeypatch.setattr(history,'headers',lambda cfg,scopes:{'Authorization':'Bearer synthetic-user'})
        calls=[]
        def server(req):
            calls.append(req.url.path)
            if req.method=='POST':
                return httpx.Response(200,json={'code':0,'data':{'items':[{'chat_id':'oc_private','chat_mode':'p2p',
                    'p2p_target_id':'ou_peer','p2p_target_type':'user'}]}})
            return httpx.Response(200,json={'code':0,'data':{'items':[{'chat_id':'oc_private','message_id':'x',
                'create_time':str(int(req.url.params['end_time'])*1000),'msg_type':'text',
                'sender':{'sender_type':'user','id':'ou_peer'},'body':{'content':json.dumps({'text':'已完成虚构交付'})}}]}})
        pub.http=httpx.Client(transport=httpx.MockTransport(server))
        def event(key,agent):
            return {'header':{'app_id':'app','event_id':key,'tenant_key':'tenant'},'event':{'sender':{'sender_type':'user',
                  'sender_id':{'open_id':'ou_owner'}},'message':{'message_id':'om_'+key,'message_type':'text','chat_type':'p2p','chat_id':'oc_boss',
                  'content':json.dumps({'text':f'请让{agent}总结我和小周最近6小时的聊天记录'})}}}
        wrong=event('wrong','渲染')
        assert receive(db,identity,wrong)
        await worker.process(db.execute("select * from inbox where event='wrong'").fetchone())
        assert not calls
        right=event('right','焊接')
        assert receive(db,identity,right)
        row=db.execute("select * from inbox where event='right'").fetchone()
        await worker.process(row)
        if cloud:
            await asyncio.gather(*worker.private_summary_tasks.values())
        assert len(calls)==2
        assert bool(model_inputs)==cloud
        job=db.execute('select * from private_history_jobs').fetchone()
        assert job['assignee']==employee.id and job['matched']==(0 if revoke else 1)
        assert job['state']==('revoked' if revoke else 'codex_completed' if cloud else 'completed')
        outputs=db.execute("select o.role,o.body from direct_finals f join outbox o on f.outgoing=o.id where f.event='right'").fetchall()
        assert len(outputs)==1 and outputs[0]['role']=='steward'
        answer=json.loads(outputs[0]['body'])
        if revoke:
            assert '已丢弃' in answer['text'] and '虚构交付' not in answer['text']
        else:
            assert '已完成虚构交付' in answer['text'] and '焊接' in answer['text']
        assert answer['reply_to']=='om_right'
        assert db.execute('select count(*) from submissions').fetchone()[0]==0
        assert not receive(db,identity,right)
        await worker.process(row)
        assert len(calls)==2
        if not revoke:
            delivered=[]
            pub.deliver=lambda item:delivered.append(json.loads(item['body'])['text']) or 'om_sent'
            cfg['history_sources']={}
            await history.deliver_ready(worker)
            assert len(delivered)==1 and '已丢弃' in delivered[0] and '虚构交付' not in delivered[0]
            assert db.execute('select count(*) from private_summary_notes').fetchone()[0]==0


def test_orphaned_cloud_attempt_reports_interruption_without_resubmitting(tmp_path):
    with closing(journal(tmp_path/'j')) as db:
        cfg={'roles':{},'steward':{'app_id':'app'}}
        worker=Worker(cfg,Publisher(cfg,db,tmp_path))
        with db:
            db.execute("insert into inbox(app,event,message,payload,state) values('app','e','om_a','{}','private_summarizing')")
            db.execute("insert into private_history_jobs(app,event,state) values('app','e','analyzing')")
        history.resume_jobs(worker)
        history.resume_jobs(worker)
        assert db.execute('select state from private_history_jobs').fetchone()[0]=='interrupted'
        assert db.execute('select count(*) from outbox').fetchone()[0]==1

def test_document_export_skips_copied_quotes_even_inside_longer_notes():
    from app.feishu.private_summary import exportable
    assert not exportable('对方提到密码1234',['06-01 10:00 我：密码1234'])
    assert not exportable('原话为abcdefghijkl', ['上一条 小周：--abcdefghijklmno--'])
    assert exportable('方案等待批准',['上一条 小周：这个方案我下周一才能确认。'])

@pytest.mark.asyncio
async def test_private_model_selection_respects_explicit_or_single_native_default(monkeypatch):
    from types import SimpleNamespace
    from app.services.codex_adapter import CodexAdapter
    from app.feishu.private_summary import select_model
    seen=[]
    catalog=[SimpleNamespace(id='gpt-5.4',default=False),SimpleNamespace(id='gpt-6.2-sol',default=True)]
    async def models(self,exe):
        seen.append(exe)
        return catalog
    monkeypatch.setattr(CodexAdapter,'_live_models',models)
    assert await select_model('verified','codex','gpt-5.4')=='gpt-5.4' and not seen
    assert await select_model('verified','codex',None)=='gpt-6.2-sol' and seen==['verified']
    # The separate contact-summary consent is for OpenAI, even when the steward chats via DeepSeek.
    assert await select_model('verified','codex','deepseek-flash')=='gpt-6.2-sol'
    assert await select_model('verified','codex','deepseek-v4-pro')=='gpt-6.2-sol'
    with pytest.raises(ValueError):
        await select_model('verified','codex','unrelated-provider-model')
    for invalid in ([],[SimpleNamespace(id='gpt-a',default=True),SimpleNamespace(id='gpt-b',default=True)]):
        catalog[:]=invalid
        with pytest.raises(ValueError):
            await select_model('verified','codex',None)
    with pytest.raises(ValueError):
        await select_model('verified','other',None)


def test_launcher_path_still_requires_official_store_package_location(tmp_path,monkeypatch):
    import os
    from app.feishu import private_summary
    if os.name!='nt':
        pytest.skip('Windows launcher')
    monkeypatch.setenv('ProgramFiles',str(tmp_path))
    official=tmp_path/'WindowsApps'/'OpenAI.Codex_1.0_x64__2p2nqsd0c76g0'/'app'/'resources'/'codex.exe'
    official.parent.mkdir(parents=True)
    official.touch()
    monkeypatch.setattr(private_summary.shutil,'which',lambda _:None)
    monkeypatch.setenv('LOCAL_CODEX_CLI_PATH',str(official))
    assert private_summary.executable()==official
    outside=tmp_path/'codex.exe'
    outside.touch()
    monkeypatch.setenv('LOCAL_CODEX_CLI_PATH',str(outside))
    with pytest.raises(ValueError):
        private_summary.executable()

def test_responses_empty_tools_encodings_and_denied_tool_declarations():
    from app.feishu.private_summary import empty_tools
    request={'model':'gpt-6.2-sol','tool_choice':'auto'}
    assert empty_tools(request,request['model'])
    assert empty_tools({**request,'tools':[]},request['model'])
    for fields in ({'tools':None},{'tools':[{'type':'web_search'}]},{'tool_choice':'required'},
                   {'tool_choice':{'type':'function','name':'shell'}},{'previous_response_id':'prior'},
                   {'functions':[]},{'conversation':'shared'}):
        assert not empty_tools({**request,**fields},request['model'])


def test_summary_network_only_uses_the_explicit_windows_loopback_proxy(monkeypatch):
    from app.feishu.private_summary import environment
    import os
    if os.name != "nt":
        pytest.skip("Windows network profile")
    monkeypatch.setenv("HTTPS_PROXY","http://untrusted.example:9000")
    monkeypatch.setenv("SSL_CERT_FILE","untrusted.pem")
    monkeypatch.setenv("SUPARAGENT_CODEX_HTTPS_PROXY","http://127.0.0.1:8118")
    assert "HTTPS_PROXY" not in environment()
    live = environment(model_request=True)
    assert live["HTTPS_PROXY"] == "http://127.0.0.1:8118"
    assert "SSL_CERT_FILE" not in live and "127.0.0.1" in live["NO_PROXY"]
    monkeypatch.setenv("SUPARAGENT_CODEX_HTTPS_PROXY","http://another.example:8118")
    with pytest.raises(ValueError):
        environment(model_request=True)
