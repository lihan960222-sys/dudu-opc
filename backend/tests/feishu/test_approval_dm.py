from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.feishu import approvals
from app.feishu.routing import journal
from app.services import thread_store as store
from tests.services.thread_harness import sessions, owner


def test_native_owner_decision_summarises_exact_operation_and_job(tmp_path):
    db = journal(tmp_path / "native")
    approvals.initialize(db)
    item = {"id":"native", "tool":"Bash", "input":{"command":"python -m pytest", "cwd":"C:/fixture"}}
    execution = {"thread":24, "turn":25, "event":"team:42", "assignment_epoch":3, "agent_id":17}
    approvals.queue_notice(db, execution, 9, item, "Jensen 的派工 #42")
    approvals.queue_notice(db, execution, 9, item, "Jensen 的派工 #42")
    [sent] = db.execute("select * from outbox").fetchall()
    assert sent["role"] == "steward" and "python -m pytest" in sent["body"]
    assert "C:/fixture" in sent["body"] and "确认" in sent["body"]
    binding = db.execute("select * from approval_dm").fetchone()
    assert binding["state"] == "pending" and binding["assignment_epoch"] == 3 and binding["agent_id"] == 17
    db.close()


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
    worker = SimpleNamespace(db=db,cfg={"steward":{"app_id":"steward","open_id":"ou_test_owner"}},
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
    with journal(tmp_path/"journal") as reopened:
        stored = reopened.execute("select decision,actor_open_id,decision_event,state from approval_dm").fetchone()
        assert tuple(stored) == (decision,"ou_test_owner","decision","answered")


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

@pytest.mark.asyncio
async def test_active_delegated_approval_reaches_steward_once(sessions, owner, tmp_path, monkeypatch):
    from app.db import session
    from app.db.models import AgentJob
    from app.services import agent_store
    from app.team.service import change_member
    monkeypatch.setattr(session, "AsyncSessionLocal", sessions)
    async with sessions() as db:
        lead = await agent_store.create_agent(db, owner_id=owner, name="Lead", make_lead=True)
        staff = await agent_store.create_agent(db, owner_id=owner, name="Employee")
        await change_member(db, owner, staff.id)
        thread = await store.create_thread(db, owner_id=owner, agent_id=staff.id, provider="codex", workspace_path=str(tmp_path))
        turn = await store.start_turn(db, thread.id, "work")
        await store.create_approval(db, thread_id=thread.id, turn_id=turn.id, request_id="a",
            tool="feature_files.write_text", input={"path":"report.txt","text":"draft"})
        db.add(AgentJob(owner_id=owner, requester_agent_id=lead.id, requester_thread_id=1,
                        requester_turn_id=1, assignee_agent_id=staff.id, assignee_thread_id=thread.id,
                        assignee_turn_id=turn.id, brief="report", brief_hash="x", status="running"))
        await db.commit()
        channel = journal(tmp_path / "journal")
        try:
            approvals.initialize(channel)
            worker = SimpleNamespace(db=channel, cfg={"owner_id":owner,"steward":{"agent_id":lead.id}})
            await approvals.collect_team_requests(worker)
            await approvals.collect_team_requests(worker)
            messages = channel.execute("select role,body from outbox").fetchall()
            assert len(messages) == 1 and messages[0]["role"] == "steward"
            assert "Employee" in messages[0]["body"] and "report.txt" in messages[0]["body"]
            junior = await agent_store.create_agent(db, owner_id=owner, name="Junior")
            await change_member(db, owner, junior.id)
            junior.manager_agent_id = staff.id
            subordinate_thread = await store.create_thread(db, owner_id=owner, agent_id=junior.id, provider="codex", workspace_path=str(tmp_path))
            subordinate_turn = await store.start_turn(db, subordinate_thread.id, "nested work")
            await store.create_approval(db, thread_id=subordinate_thread.id, turn_id=subordinate_turn.id,
                request_id="b", tool="feature_files.write_text", input={"path":"nested.txt","text":"draft"})
            db.add(AgentJob(owner_id=owner, requester_agent_id=staff.id, requester_thread_id=thread.id,
                requester_turn_id=turn.id, assignee_agent_id=junior.id, assignee_thread_id=subordinate_thread.id,
                assignee_turn_id=subordinate_turn.id, brief="nested", brief_hash="y", status="running"))
            await db.commit()
            await approvals.collect_team_requests(worker)
            assert channel.execute("select count(*) from outbox").fetchone()[0] == 1
        finally:
            channel.close()
