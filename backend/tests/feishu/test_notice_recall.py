import asyncio
import json
from contextlib import closing
from types import SimpleNamespace

import httpx

from app.feishu.publisher import Publisher
from app.feishu.routing import enqueue, journal
from app.feishu.worker import Worker


def test_only_delivered_notices_of_answered_event_recalled_after_restart(tmp_path):
    path = tmp_path/'channel'
    sent, recalls = {}, []
    def send(row):
        sent[row['id']] = 'om_' + row['id']
        return sent[row['id']]
    def recall(remote):
        recalls.append(remote)
    def incoming(db,event):
        row = {'app':'app','event':event,'message':'om_input_'+event}
        db.execute("insert into inbox(app,event,message) values(:app,:event,:message)",row)
        return row
    def worker(db):
        return Worker({'steward':{'app_id':'app'}},SimpleNamespace(db=db,deliver=send,recall_notice=recall))
    with closing(journal(path)) as db:
        w = worker(db)
        a,b = incoming(db,'a'),incoming(db,'b')
        w.direct_notice(a,'queued','排队中')
        w.direct_notice(b,'running','思考中')
        w.flush()
        w.direct_notice(a,'running','思考中')
        w.flush()
        old = enqueue(db,None,'steward',{'text':'思考中','reply_to':a['message']})
        db.execute("insert into direct_notices values('app','old','running',?)",(old,))
        w.flush()
        a_notices = {r[0] for r in db.execute("select o.remote from outbox o join direct_notices n on n.outgoing=o.id where n.event='a'")}
        w.direct_final(a,'done','final answer')
        assert not recalls
        # Unconfirmed final delivery leaves the notices visible.
        w.pub.deliver = lambda row: (_ for _ in ()).throw(httpx.ReadTimeout('uncertain'))
        w.flush()
        assert not recalls
        db.execute("update outbox set state='pending' where state='verify'")
        db.commit()
    with closing(journal(path)) as db:
        w = worker(db)
        w.flush()
        w.flush()
        assert set(recalls)==a_notices and len(recalls)==2
        assert db.execute("select count(*) from outbox where body like '%final answer%' and state='sent'").fetchone()[0]==1
        w.direct_final({'app':'app','event':'old','message':'om_old'},'done','other answer')
        w.flush()
        assert set(recalls)==a_notices


def test_publisher_recalls_using_steward_identity(tmp_path):
    with closing(journal(tmp_path/'db')) as db:
        pub = Publisher({'steward':{'app_id':'steward'}},db,tmp_path)
        pub.headers = lambda role: {'Authorization':role['app_id']}
        def transport(request):
            assert request.method=='DELETE' and request.url.path=='/open-apis/im/v1/messages/om_notice'
            assert request.headers['Authorization']=='steward'
            return httpx.Response(200,json={'code':0})
        with httpx.Client(transport=httpx.MockTransport(transport)) as pub.http:
            pub.recall_notice('om_notice')
