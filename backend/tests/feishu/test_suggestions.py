import json
from contextlib import closing

import httpx
from app.feishu import suggestions
from app.feishu.ingress import receive
from app.feishu.publisher import Publisher
from app.feishu.routing import journal
from app.feishu.worker import Worker


def test_restarting_does_not_send_another_batch_until_two_hours_after_delivery(tmp_path):
    cfg={'proactive_suggestions':True,'steward':{'app_id':'boss'}}
    reads=[]
    def pending(req):
        reads.append(req.url.path)
        return httpx.Response(200,json={'items':[dict(id=n,revision=1,state='pending',title=str(n),
            detail='example') for n in range(5)]})
    def check(at):
        with closing(journal(tmp_path/'j')) as db:
            worker=Worker(cfg,Publisher(cfg,db,tmp_path))
            with httpx.Client(base_url='http://local/',transport=httpx.MockTransport(pending)) as api:
                worker.api=api
                suggestions.poll(worker,wall_now=at)
            return db.execute('select count(*) from suggestion_offers').fetchone()[0]
    assert check(0)==3
    with closing(journal(tmp_path/'j')) as db:
        created=db.execute('select max(created) from outbox').fetchone()[0]
    assert check(created+7500)==3  # Network outage: those offers still await delivery.
    with closing(journal(tmp_path/'j')) as db, db:
        db.execute("update outbox set state='sent'")
        suggestions.delivered(db,db.execute('select outgoing from suggestion_offers limit 1').fetchone()[0])
        # Force a delayed acknowledgement from Feishu using the test clock.
        db.execute('update suggestion_delivery_clock set sent=?',(created+7500,))
    assert check(created+7500+7199)==3
    assert check(created+7500+7200)==5
    assert len(reads)==2


def test_bound_suggestion_and_idempotent_decision(tmp_path):
    cfg={'proactive_suggestions':True,'steward':{'app_id':'boss'}}
    with closing(journal(tmp_path/'j')) as db:
        w=Worker(cfg,Publisher(cfg,db,tmp_path))
        calls=[]
        def backend(req):
            if req.method=='GET':
                return httpx.Response(200,json={'items':[{'id':4,'revision':2,'state':'pending','title':'核对单位','detail':'检查量纲','task_id':None}]})
            calls.append(json.loads(req.content))
            return httpx.Response(200,json={'state':'approved'})
        w.api=httpx.Client(base_url='http://localhost/api/v1/',transport=httpx.MockTransport(backend))
        suggestions.poll(w)
        suggestions.poll(w)
        assert db.execute('select count(*) from outbox').fetchone()[0]==1
        db.execute("update outbox set state='sent',remote='om_offer'")
        row={'app':'boss','event':'answer','message':'om_a'}
        msg={'chat_type':'p2p','message_type':'text','parent_id':'om_offer'}
        db.execute("insert into inbox(app,event,message) values(:app,:event,:message)",row)
        assert not suggestions.answer(w,{**row,'app':'specialist'},msg,'采纳')
        assert suggestions.answer(w,row,msg,'采纳')
        assert suggestions.answer(w,row,msg,'采纳')
        assert calls==[{'choice':'approve','revision':2}]

def test_numbered_choice_replayed_after_timeout(tmp_path):
    cfg={'proactive_suggestions':True,'steward':{'app_id':'boss'}}
    with closing(journal(tmp_path/'j')) as db:
        worker=Worker(cfg,Publisher(cfg,db,tmp_path))
        request_bodies=[]
        def api(req):
            if req.method=='GET':
                return httpx.Response(200,json={'items':[{'id':'s1','revision':3,'state':'pending','title':'检查','detail':'见记录',
                    'options':[{'id':'brief','label':'整理摘要'},{'id':'followup','label':'建后续任务'}]}]})
            request_bodies.append(json.loads(req.content))
            if len(request_bodies)==1:
                raise httpx.ReadTimeout('lost')
            return httpx.Response(200,json={'state':'approved','report':'已登记','followup_task_id':81})
        worker.api=httpx.Client(base_url='http://localhost/',transport=httpx.MockTransport(api))
        suggestions.poll(worker)
        db.execute("update outbox set state='sent',remote='om_s'")
        msg={'chat_type':'p2p','parent_id':'om_s','message_type':'text','content':'{"text":"2"}'}
        db.execute("insert into inbox(app,event,message,payload) values('boss','e','om_choice',?)",(json.dumps(msg),))
        row=db.execute('select * from inbox').fetchone()
        suggestions.answer(worker,row,msg,'2')
        assert db.execute('select state from inbox').fetchone()[0]=='suggestion_deciding'
        suggestions.answer(worker,row,msg,'2')
        assert request_bodies==[{'choice':'approve','revision':3,'option':'followup'}]*2
        assert '#81' in json.loads(db.execute('select body from outbox order by rowid desc limit 1').fetchone()[0])['text']

def test_two_patrol_signals_produce_one_short_actionable_reply(tmp_path):
    cfg = {'proactive_suggestions': True, 'steward': {'app_id': 'boss'}}
    data = [
        ('有任务在等待', 'Omniverse 开发现状盘点（只读）'),
        ('有成果等待验收', '焊接方向与 Omniverse 的接口/依赖盘点（只读）'),
    ]
    items = [dict(id=str(i), revision=1, state='pending', title=f'{status} · {title}',
                  detail='验收条件和原始任务长文' * 100,
                  options=[dict(id='brief',label='摘要'),dict(id='followup',label='跟进')])
             for i, (status, title) in enumerate(data, 1)]
    decisions = []
    def backend(req):
        if req.method == 'GET':
            return httpx.Response(200,json={'items':items})
        decisions.append((req.url.path, json.loads(req.content)))
        return httpx.Response(200,json={'followup_task_id':81,'report':'详细记录'*300})
    with closing(journal(tmp_path/'j')) as db:
        worker = Worker(cfg,Publisher(cfg,db,tmp_path))
        with httpx.Client(base_url='http://local/', transport=httpx.MockTransport(backend)) as api:
            worker.api = api
            suggestions.poll(worker)
            messages = db.execute('select body from outbox').fetchall()
            assert len(messages) == 1
            text = json.loads(messages[0]['body'])['text']
            assert '等待中' in text and '待验收' in text and '跟进1' in text
            assert len(text) < 135 and '验收条件' not in text
            db.execute("update outbox set state='sent',remote='om_digest'")
            msg = {'chat_type':'p2p','message_type':'text','parent_id':'om_digest'}
            row = {'app':'boss','event':'reply','message':'om_choice'}
            db.execute("insert into inbox(app,event,message) values(:app,:event,:message)",row)
            assert suggestions.answer(worker,row,msg,'跟进2')
            assert decisions == [('/steward/suggestions/2/decision',{'choice':'approve','revision':1,'option':'followup'})]
            assert len(json.loads(db.execute("select body from outbox where state='pending'").fetchone()[0])['text']) < 30
