import json
import sqlite3
import pytest

from app.db.models import AgentJob, AgentJobEvent, AgentJobInbox, FeatureMessage, WorkItem
from app.feishu import dispatch_recovery as recovery
from app.feishu import unpublished_retry
from app.services import feature_tasks, thread_store
from tests.feishu.test_dispatch_recovery import setup_case  # noqa: F401
from tests.services.thread_harness import sessions, owner  # noqa: F401


def test_original_read_must_match_the_same_invocation_chat_and_preexisting_document(tmp_path):
    db=sqlite3.connect(":memory:")
    db.executescript("""create table employee_history_reads(app,employee,job,chat,cutoff,frontier);
        create table knowledge_documents(binding primary key,document);
        insert into employee_history_reads values('app',5,'inv','chat',12,3);
        insert into knowledge_documents values('bound','oldDoc');""")
    scope={"app":"app","chat":"chat","binding":"bound","title":"Document","origin":"https://tenant.feishu.cn"}
    observed={"title":"Document","remote_state":"missing","url":None}
    path=tmp_path/"knowledge-task.json"
    path.write_text("{}")
    assert unpublished_retry.prior_resource(db,scope,5,"inv",3,observed,path,0)
    with pytest.raises(recovery.RecoveryDenied):
        unpublished_retry.prior_resource(db,{**scope,"chat":"replacement"},5,"inv",3,observed,path,0)
    with pytest.raises(recovery.RecoveryDenied):
        unpublished_retry.prior_resource(db,{**scope,"binding":"changed"},5,"inv",3,observed,path,0)
    db.execute("insert into knowledge_documents values('different','otherDoc')")
    with pytest.raises(recovery.RecoveryDenied):
        unpublished_retry.prior_resource(db,scope,5,"inv",3,observed,path,0)
    assert unpublished_retry.prior_resource(db,scope,5,"inv",3,
        {**observed,"remote_state":"present","url":scope["origin"]+"/docx/oldDoc"},path,0)


def test_delayed_second_rearm_cannot_queue_another_final_after_delivery():
    db=sqlite3.connect(":memory:")
    db.executescript("""create table direct_team_reports(app,event,job,finished);
        create table direct_finals(app,event,outgoing); insert into direct_finals values('a','e','old');
        insert into direct_team_reports values('a','e',9,1);""")
    with db:
        unpublished_retry.rearm_once(db,"a","e",9,"original-attempt","old")
    with db:
        db.execute("update direct_team_reports set finished=1")
        db.execute("update direct_finals set outgoing='new-confirmed'")
    with db:
        unpublished_retry.rearm_once(db,"a","e",9,"original-attempt","old")
    assert db.execute("select finished from direct_team_reports").fetchone()==(1,)


def test_incomplete_or_unknown_host_effect_cannot_be_replayed():
    start = ("tool.started", {"call_id":"cmd", "name":"Bash"})
    declined = ("tool.completed", {"call_id":"cmd", "ok":False, "command_receipt":{"status":"declined","exit_code":None}})
    reads = [("tool.started", {"call_id":"r", "name":"mcp__team__feishu_read_history"})]
    assert unpublished_retry.only_reads([*reads, start, declined])
    assert not unpublished_retry.only_reads([*reads, start])
    assert not unpublished_retry.only_reads([start, ("tool.completed",{"call_id":"cmd","ok":False,"output":"(declined)"})])
    for name in ("Bash", "mcp__team__feishu_publish_knowledge", "mcp__unknown__dispatch"):
        assert not unpublished_retry.only_reads([*reads, ("tool.started",{"call_id":"u","name":name})])


@pytest.mark.parametrize("change", ["folder","actor","revoked","registered_document","before_body"])
async def test_publish_uses_only_the_original_resource_and_rechecks_before_writing(monkeypatch,change,tmp_path):
    from types import SimpleNamespace
    from app.feishu import document_access, knowledge_document as knowledge, knowledge_revision
    cfg={"steward":{"app_id":"lead","open_id":"owner","tenant_key":"t"},
         "_access_config_path":str(tmp_path/"config.json"),"state_dir":"state"}
    identity={"_employee_oauth":True,"steward":{"app_id":"staff","open_id":"original-actor"}}
    source={"agent_id":4,"chat_id":"chat","oauth_app":"staff","peer_open_id":"peer"}
    dest={"folder_token":"folder","tenant_origin":"https://example.feishu.cn"}
    frozen=unpublished_retry.outbound_scope(cfg,4,"contact",source,dest,"title",identity,"openai")
    actual_dest={**dest,"folder_token":"changed"} if change=="folder" else dest
    actual_actor={**identity,"steward":{**identity["steward"],"open_id":"changed"}} if change=="actor" else identity
    decisions=iter([change!="revoked",False])
    async def authorized(): return next(decisions)
    proof={"runtime_vendor":"openai","_retry_resource":frozen,"_retry_document":"oldDoc","_retry_authorized":authorized}
    db=sqlite3.connect(":memory:")
    db.execute("create table knowledge_documents(binding primary key,document)")
    db.execute("insert into knowledge_documents values(?,?)",(frozen["binding"],"changed" if change=="registered_document" else "oldDoc"))
    async def reviewer(*args): return (4,"staff","codex","gpt-test")
    monkeypatch.setattr(knowledge,"target",lambda *args:(actual_dest,"title"))
    monkeypatch.setattr(knowledge.history,"reviewer",reviewer)
    monkeypatch.setattr(knowledge.history,"source_config",lambda *args:actual_actor)
    monkeypatch.setattr(knowledge.workspace_access,"settings_for",lambda *args:{"history_sources":{"contact":source},"allow_codex_summary":True})
    monkeypatch.setattr(knowledge.history,"headers",lambda *args:{})
    monkeypatch.setattr(knowledge.history,"peer_for",lambda *args:"peer")
    monkeypatch.setattr(knowledge,"collect",lambda *args:{"m":{}})
    resolved=[]
    def resolve(*args,**kwargs):
        resolved.append(True)
        return "oldDoc"
    monkeypatch.setattr(knowledge,"resolve_document",resolve)
    monkeypatch.setattr(document_access,"verify_destination",lambda *args:None)
    async def revise(db,client,headers,authorized,*args,**kwargs):
        assert not await authorized()
        raise ValueError("撤回")
    monkeypatch.setattr(knowledge_revision,"revise_document",revise)
    with pytest.raises(ValueError):
        await knowledge.publish(SimpleNamespace(cfg=cfg,db=db,pub=SimpleNamespace(http=None)),{"app":"lead","event":"original"},
            [None,"staff",None,"contact"],source,12,revise=True,employee_draft=lambda _:["draft"],assignment=proof)
    assert bool(resolved)==(change=="before_body")
    db.close()


@pytest.mark.parametrize("change", ["none","attempted","other_worker","stale_turn","new_message","changed_folder_before_release",
                                    "other_invocation","new_contact","no_old_document","another_retry"])
async def test_retry_reuses_original_card_and_rearms_only_its_sent_reply(setup_case,sessions,change,monkeypatch):
    cfg, lead_thread, owner_turn, task, employee, _ = setup_case
    initiation = await recovery.recover(sessions,cfg,owner_turn,task,apply=True,require_document=True)
    owner_id = cfg["owner_id"]
    key = recovery.event_key({"app":"test-app","event":"test-event"})
    async with sessions() as db:
        parent = await db.get(AgentJob,initiation["root"])
        parent.status, parent.outcome = "failed", "missing_delivery"
        card = await db.get(WorkItem,task)
        chat,_ = await feature_tasks.session_for(db,owner_id,task,model="synthetic",workspace_path=cfg["state_dir"])
        turn = await thread_store.start_turn(db,chat.id,"employee task",internal=True)
        for name,body in (("feishu_list_sources",{"knowledge_target":{"title":"Test document","remote_state":"missing","url":None}}),
                          ("feishu_read_history",{"source_complete":True,"total":3})):
            await thread_store._add_event(db,chat.id,"tool.started",{"call_id":name,"name":"mcp__team__"+name},turn.id)
            await thread_store._add_event(db,chat.id,"tool.completed",{"call_id":name,"ok":True,"output":json.dumps(body)},turn.id)
        await thread_store.finish_turn(db,turn.id,turn.owner_token,status="succeeded",result="BLOCKED: no publication")
        child = AgentJob(owner_id=owner_id,kind="job",mode="work",reply="resume",origin_kind="person",
            root_id=parent.id,parent_id=parent.id,requester_thread_id=lead_thread,requester_agent_id=parent.assignee_agent_id,
            requester_turn_id=owner_turn,assignee_agent_id=employee,assignee_thread_id=chat.id,assignee_turn_id=turn.id,
            task_id=task,project="other-project",assignment_epoch=1,brief="Retain original brief",brief_hash="hash",
            status="failed",outcome="verification_blocked",approval_mode="ask")
        db.add(child)
        await db.flush()
        card.thread_id=chat.id
        for kind, detail in (("owner_request",{"turn_id":owner_turn,"thread_id":lead_thread,"event_key":key}),
                ("feishu_assignment",{"action":"employee","employee":employee,"contact":"test contact"})):
            db.add(AgentJobEvent(job_id=child.id,kind=kind,detail=detail))
        await db.flush()
        from app.team.feishu_tools_bridge import invocation_key
        operation=await invocation_key(db,child,{})
        db.add(AgentJobEvent(job_id=child.id,kind="feishu_delivered",detail={"tool":"feishu_read_history",
            "employee":employee,"epoch":1,"operation":operation,"result":{"messages_read":3}}))
        db.add(AgentJobInbox(owner_id=owner_id,holder_id=parent.id,job_id=child.id,kind="settled",delivered_at=turn.finished_at))
        if change=="attempted":
            db.add(AgentJobEvent(job_id=child.id,kind="feishu_tool_started",detail={"tool":"feishu_publish_knowledge"}))
        if change=="other_worker":
            card.agent_id=parent.assignee_agent_id
        if change=="another_retry":
            db.add(AgentJobEvent(job_id=child.id,kind=unpublished_retry.RETRY_EVENT,detail={"previous_turn":turn.id-1}))
        if change=="stale_turn":
            later=await thread_store.start_turn(db,chat.id,"later",internal=True)
            await thread_store.finish_turn(db,later.id,later.owner_token,status="succeeded")
        await db.commit()
    with recovery.channel(cfg,writable=True) as journal,journal:
        journal.execute("create table employee_history_reads(app,employee,job,chat,cutoff,frontier)")
        journal.execute("create table knowledge_documents(binding primary key,document)")
        journal.execute("insert into employee_history_reads values(?,?,?,?,4,3)",("employee-app",employee,
            operation if change!="other_invocation" else "different", "old-chat" if change!="new_contact" else "new-chat"))
        if change!="no_old_document":
            journal.execute("insert into knowledge_documents values('registered','oldDocument')")
        journal.execute("update direct_team_reports set finished=1 where job=?",(parent.id,))
        journal.execute("insert into assignment_outbox(job,outgoing) values(?,'old-final')",(child.id,))
        if change=="new_message":
            journal.execute("insert into inbox(app,event,message,payload,state) values('test-app','later','later', '{}','done')")
            journal.execute("insert into owner_dm_sources values('test-app','later','later','test-owner','tenant','test-chat')")
    scope={"binding":"registered","app":"employee-app","chat":"old-chat","title":"Test document",
           "origin":"https://test.feishu.cn","vendor":"openai"}
    monkeypatch.setattr(unpublished_retry,"resource_scope",lambda *_:scope)
    args=(sessions,cfg,owner_turn,task,turn.id)
    if change=="changed_folder_before_release":
        snapshots = iter([scope,{**scope,"binding":"another-folder"}])
        monkeypatch.setattr(unpublished_retry,"resource_scope",lambda *_:next(snapshots))
    if change!="none":
        with pytest.raises(recovery.RecoveryDenied):
            await unpublished_retry.retry(*args,apply=True)
        async with sessions() as db:
            assert (await db.get(AgentJob,child.id)).status=="failed"
            assert (await db.get(AgentJob,parent.id)).status=="failed"
        with recovery.channel(cfg) as journal:
            assert journal.execute("select finished from direct_team_reports where job=?",(parent.id,)).fetchone()[0]==1
        return
    assert (await unpublished_retry.retry(*args))["eligible"]
    done=await unpublished_retry.retry(*args,apply=True)
    assert done["employee_job"]==child.id and done["state"]=="queued"
    assert (await unpublished_retry.retry(*args,apply=True))["already_registered"]
    async with sessions() as db:
        live=await db.get(AgentJob,child.id)
        assert (live.task_id,live.assignee_thread_id,live.requester_turn_id,live.assignee_turn_id)==(task,chat.id,owner_turn,None)
        assert live.approval_mode=="ask" and live.status=="queued"
        assert (await db.get(AgentJob,parent.id)).status=="waiting"
        assert (await db.get(FeatureMessage,(owner_id,live.origin_ref))).result["job_id"]==child.id
        role=await unpublished_retry.service.member(db, owner_id, employee)
        assert await unpublished_retry.tool_proof(db,cfg,role,live,{},"deepseek") is None
        assert (await unpublished_retry.tool_proof(db,cfg,role,live,{},"openai"))["_retry_document"]=="oldDocument"
        monkeypatch.setattr(unpublished_retry,"resource_scope",lambda *_:{**scope,"chat":"new-contact"})
        assert await unpublished_retry.tool_proof(db,cfg,role,live,{},"openai") is None
        assert (await db.get(type(turn),turn.id)).result=="BLOCKED: no publication"
    with recovery.channel(cfg) as journal:
        assert journal.execute("select finished from direct_team_reports where job=?",(parent.id,)).fetchone()[0]==0
        assert journal.execute("select count(*) from outbox").fetchone()[0]==1
