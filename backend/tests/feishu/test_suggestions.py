import json
from contextlib import closing

import httpx
from app.feishu import suggestions
from app.feishu.ingress import receive
from app.feishu.publisher import Publisher
from app.feishu.routing import journal
from app.feishu.worker import Worker


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
