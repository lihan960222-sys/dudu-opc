from datetime import timedelta
from types import SimpleNamespace

import httpx
import json
import pytest

from app.feishu import approvals
from app.feishu.routing import journal
from app.services import thread_store as store
from tests.services.thread_harness import cleanup_jobs, sessions, owner


def test_remember_requires_authenticated_owner_reply_to_exact_notice(tmp_path, monkeypatch):
    cfg = {"owner_id":"local-owner","state_dir":"channel","steward":{"app_id":"app","tenant_key":"tenant","open_id":"boss"}}
    config_path = tmp_path/"cfg.json"
    config_path.write_text(json.dumps(cfg))
    monkeypatch.setenv("FEISHU_CONFIG",str(config_path))
    folder = tmp_path/"channel"
    folder.mkdir()
    db = journal(folder/"channel.sqlite3")
    try:
        approvals.initialize(db)
        item = {"id":"req","tool":"Bash","input":{"command":"Get-Location","cwd":"C:/fixture"},"can_remember":True}
        execution = {"event":"team:2","thread":20,"turn":30,"agent_id":2,"assignment_epoch":1}
        approvals.queue_notice(db,execution,1,item,"Jensen")
        with db:
            db.execute("update outbox set state='sent', remote='notice'")
            db.execute("update approval_dm set state='verify',actor_open_id='boss',decision_event='ev',decision='allow'")
            msg = {"chat_type":"p2p","message_type":"text","parent_id":"notice","content":json.dumps({"text":"允许"})}
            db.execute("insert into inbox(app,event,message,payload) values('app','ev','reply',?)",(json.dumps(msg),))
        check = lambda: approvals.confirmed_owner_grant("local-owner","req",20,30,2,1)
        assert not check()
        with db:
            db.execute("insert into owner_dm_sources values('app','ev','reply','boss','tenant','private')")
        assert check()
        assert not approvals.confirmed_owner_grant("local-owner","req",20,30,9,1)
        with db:
            db.execute("update inbox set payload=?",(json.dumps({**msg,"parent_id":"other"}),))
        assert not check()
    finally:
        db.close()


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


@pytest.mark.parametrize("case,text,send", [
    ("one","允许",True),("one","拒绝",True),("one","仅本次",True),("one","帮我审批",False),
    ("one","帮我审批啊",False),("one","大嘴，麻烦你帮我审批一下呗！",False),
    ("many","允许",False),("gone","允许",False),("undelivered","允许",False),
    ("stale","允许",False),("unbound","允许",False),("early","允许",False),
])
async def test_unquoted_approval_checks_owner_delivery_time_and_live_requests(tmp_path, monkeypatch, case, text, send):
    import time
    db = journal(tmp_path / "gateway")
    approvals.initialize(db)
    steward = {"app_id":"app","tenant_key":"tenant","open_id":"boss"}
    item = {"id":"abc","tool":"Bash","input":{"command":"dir","cwd":"C:/work"},"can_remember":True}
    approvals.queue_notice(db, {"thread":2,"turn":3,"event":"team:4","agent_id":5,"assignment_epoch":1}, 4, item, "Jensen")
    outgoing = db.execute("select id from outbox").fetchone()[0]
    if case != "undelivered":
        db.execute("update outbox set state='sent',remote='parent'")
        approvals.delivered(db, outgoing, steward)
    now = time.time()
    db.execute("update approval_dm set delivered_at=?",(now-(1200 if case=="stale" else 2),))
    db.execute("insert into owner_dm_sources values('app','e','m',?,'tenant','oc')",
               ("other" if case=="unbound" else "boss",))
    live = [] if case == "gone" else [("abc",2,3)]
    if case == "many":
        live.append(("other",4,5))  # Another real request has not reached Feishu.
    async def list_live(owner):
        assert owner == "owner"
        return live
    monkeypatch.setattr(approvals, "_live_owner_requests", list_live)
    calls, replies = [], []
    def backend(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200,json={})
    with httpx.Client(transport=httpx.MockTransport(backend),base_url="http://localhost") as api:
        worker = SimpleNamespace(db=db,api=api,cfg={"steward":steward,"owner_id":"owner"},
                                 respond=lambda *args: replies.append(args[2]))
        msg = {"chat_id":"oc","message_id":"m","message_type":"text","chat_type":"p2p",
               "create_time":str(int((now-(3 if case=="early" else 0))*1000))}
        assert await approvals.owner_request(worker,{"app":"app","event":"e"},msg,text)
    assert len(calls) == int(send)
    if send:
        assert calls[0]["remember"] is False
        assert calls[0]["decision"] == ("deny" if text=="拒绝" else "allow")
    if "审批" in text:
        assert "dir" in replies[0] and "Jensen" in replies[0]
    if case=="many":
        assert "2 项" in replies[0]
    db.close()


async def test_reconcile_accepts_sqlite_execution_and_defers_native_card(tmp_path):
    import hashlib
    from app.feishu.worker import Worker
    db = journal(tmp_path/"channel")
    approvals.initialize(db)
    db.execute("create table executions(event text primary key,thread integer,turn integer)")
    db.execute("insert into executions values(?,2,3)",(hashlib.sha256(b"app|event").hexdigest(),))
    row={"app":"app","event":"event","task":8}
    def backend(request):
        if request.url.path.endswith("/approvals"):
            return httpx.Response(200,json=[
                {"id":"native","tool":"Bash","input":{"command":"echo","cwd":"C:/job"},
                 "employee_rememberable":False,"turn_id":3,"status":"pending"},
                {"id":"legacy","tool":"feature_files.write_text","input":{"path":"out.txt","text":"draft"},
                 "employee_rememberable":None,"turn_id":3,"status":"pending"}])
        return httpx.Response(200,json={"turns":[{"id":3,"finished_at":None}]})
    with httpx.Client(transport=httpx.MockTransport(backend),base_url="http://localhost") as api:
        await Worker.reconcile(SimpleNamespace(db=db,api=api),row)
    assert [r[0] for r in db.execute("select approval from approval_dm")]==["legacy"]
    db.close()


@pytest.mark.parametrize("remote,after,feedback", [
    ("pending","pending","仍在等待"),("allowed","answered","允许"),("denied","answered","拒绝"),
    ("expired","expired","过期"),("cancelled","expired","取消"),(None,"unconfirmed","暂时查不到")])
def test_conflict_reads_real_approval_before_answering(tmp_path, remote, after, feedback):
    db = journal(tmp_path/"channel")
    approvals.initialize(db)
    item={"id":"a","tool":"Bash","input":{"command":"cmd","cwd":"C:/role"}}
    execution={"thread":2,"turn":3,"assignment_epoch":5,"agent_id":6,"event":"team:4"}
    approvals.queue_notice(db,execution,8,item,"Jensen")
    db.execute("update outbox set remote='om_approval',state='sent'")
    calls=[]
    def backend(request):
        calls.append(request.method)
        if request.method=="POST":
            return httpx.Response(409,json={"detail":{"error_code":"approval_not_waiting"}})
        rows=[{"id":"other","turn_id":3,"status":"allowed"},{"id":"a","turn_id":9,"status":"expired"}]
        return httpx.Response(200,json=rows+([{"id":"a","turn_id":3,"status":remote}] if remote else []))
    messages=[]
    with httpx.Client(transport=httpx.MockTransport(backend),base_url="http://localhost") as api:
        worker=SimpleNamespace(db=db,api=api,cfg={"steward":{"app_id":"app","open_id":"owner"}},
                               respond=lambda *args:messages.append(args[2]))
        assert approvals.answer(worker,{"event":"e","app":"app"},{"parent_id":"om_approval","chat_type":"p2p",
                               "message_type":"text"},"仅本次")
    assert feedback in messages[0] and calls==["POST","GET"]
    assert db.execute("select state from approval_dm").fetchone()[0]==after
    db.close()


@pytest.mark.parametrize("outgoing_state", ["pending", "sent"])
def test_live_local_notice_becomes_one_exact_multiline_card(tmp_path, outgoing_state):
    from app.services.steward_reply import LIMIT
    cmd = "Get-Item '.\\x'\r\n\tWrite-Output 'line\\n (literal)'"
    item = {"id":"a","tool":"Bash","input":{"command":cmd, "cwd":"C:/work","argv":["-Command",cmd]}}
    execution = {"thread":2,"turn":3,"event":"team:4","assignment_epoch":5,"agent_id":6}
    db = journal(tmp_path/"card")
    approvals.initialize(db)
    with db:
        db.execute("insert into outbox(id,task,role,body,state) values('old',1,'steward','{}',?)",(outgoing_state,))
        db.execute("""insert into approval_dm(approval,thread,turn,dispatch,outgoing,assignment_epoch,agent_id,state)
            values('a',2,3,'team:4','old',5,6,'local')""")
    approvals.queue_notice(db, {**execution, "turn":4}, 1, item, "Jensen")
    assert db.execute("select count(*) from outbox").fetchone()[0] == 1
    approvals.queue_notice(db, execution, 1, item, "Jensen")
    approvals.queue_notice(db, execution, 1, item, "Jensen")
    old = db.execute("select state from outbox where id='old'").fetchone()[0]
    assert old == ("superseded" if outgoing_state=="pending" else "sent")
    [new] = db.execute("select body from outbox where id!='old'").fetchall()
    notice = json.loads(new["body"])
    assert notice["approval_card"] == "a" and len(notice["text"]) <= LIMIT
    encoded = next(line.split("：",1)[1] for line in notice["text"].splitlines() if "JSON 字符串" in line)
    assert json.loads(encoded) == cmd
    assert db.execute("select state from approval_dm where approval='a'").fetchone()[0]=="pending"
    db.close()


@pytest.mark.parametrize("bad",["echo 1\npassword=example", "echo 1\n\x1b[31mabc", "echo 1\n\u202eabc"])
def test_multiline_approval_still_protects_sensitive_or_invisible_input(bad):
    for args in ({"command":bad,"cwd":"C:/w"}, {"command":"dir","cwd":"C:/w","argv":[bad]}):
        assert approvals.describe({"tool":"Bash","input":args},native=True) is None


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
    replies = []
    worker = SimpleNamespace(db=db,cfg={"steward":{"app_id":"steward","open_id":"ou_test_owner"}},
        api=httpx.Client(transport=httpx.MockTransport(backend),base_url="http://localhost"),
        respond=lambda *args: replies.append(args[2]))
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
    assert replies[-1] == ("老板，我已按您的选择允许本次操作。" if decision == "allow" else "老板，我已按您的选择拒绝本次操作。")
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
async def test_active_delegated_approval_reaches_steward_once(sessions, owner, cleanup_jobs, tmp_path, monkeypatch):
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
