from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.feishu import approvals
from app.feishu.routing import journal
from app.services import thread_store as store
from tests.services.thread_harness import sessions, owner


@pytest.mark.parametrize("reply, decision", [("确认", "allow"), ("拒绝", "deny")])
def test_exact_private_approval_reply(tmp_path, reply, decision):
    db = journal(tmp_path/"journal")
    approvals.initialize(db)
    calls = []
    def backend(req):
        if req.method == "GET":
            item = dict(id="a1",turn_id=3,status="pending",tool="feature_files.write_text",
                        input={"path":"结果/out.txt","text":"private contents"})
            return httpx.Response(200,json=[item,{**item,"id":"old","turn_id":2}])
        import json
        calls.append((req.url.path,json.loads(req.content)))
        return httpx.Response(200,json={"decision":decision})
    worker = SimpleNamespace(db=db,cfg={"steward":{"app_id":"steward"}},
        api=httpx.Client(transport=httpx.MockTransport(backend),base_url="http://localhost"),
        respond=lambda *args: None)
    execution = dict(thread=2,turn=3,event="source")
    approvals.notify(worker,execution,4)
    approvals.notify(worker,execution,4)
    notices = db.execute("select * from outbox").fetchall()
    assert len(notices)==1 and notices[0]["role"]=="steward"
    assert "结果/out.txt" in notices[0]["body"] and "private contents" not in notices[0]["body"]
    db.execute("update outbox set remote='m1', state='sent'")
    row = dict(app="steward",event="decision")
    msg = dict(chat_type="p2p",message_type="text",parent_id="m1")
    assert not approvals.answer(worker,row,{**msg,"chat_type":"group"},reply)
    assert approvals.answer(worker,row,{**msg,"parent_id":"unrelated"},reply)
    assert not calls
    assert approvals.answer(worker,row,msg,reply)
    assert approvals.answer(worker,{**row,"event":"retry"},msg,reply)
    assert calls == [("/threads/2/approvals/a1",{"decision":decision,"remember":False,"expected_turn_id":3})]
    db.close()


@pytest.mark.asyncio
async def test_decision_requires_exact_live_lease(sessions, owner, tmp_path):
    async with sessions() as db:
        thread = await store.create_thread(db,owner_id=owner,provider="codex",workspace_path=str(tmp_path))
        turn = await store.start_turn(db,thread.id,"work")
        approval = await store.create_approval(db,thread_id=thread.id,turn_id=turn.id,request_id="r",
            tool="feature_files.write_text",input={"path":"out.txt","text":"sample"})
        aid, tid = approval.id, turn.id
        now = store.utcnow()
        args = dict(expected_turn_id=turn.id, owner_token=turn.owner_token, fresh_after=now-timedelta(seconds=60))
        for override in ({"owner_token":"stale"}, {"fresh_after":now+timedelta(seconds=5)},
                         {"expected_turn_id":tid+1}):
            assert not await store.decide_approval(db,aid,"allowed",**{**args,**override})
        assert await store.decide_approval(db,aid,"allowed",**args)
        assert not await store.decide_approval(db,aid,"denied",**args)
