"""Only a recorded, rejected employee handoff may be resumed by its local operator."""
import json
from contextlib import closing
from types import SimpleNamespace

import pytest

from app.feishu import dispatch_recovery as recovery
from app.core.config import settings
from app.db.models import AgentJob, Approval, Event, FeishuLeadChat, Turn
from app.feishu import routing
from app.services import agent_store, feature_tasks, thread_store
from app.team import feishu_assignment, service
from sqlalchemy import select
from tests.services.thread_harness import sessions, owner


def tool_pair(arguments, *, code="assignment_context", call="one", name="mcp__team__delegate"):
    output = {"code":code,"error":"任务编号不属于当前老板或项目。",
              "receipts":[{"agent_id":arguments["agent_ids"][0],"code":code,"error":"任务编号不属于当前老板或项目。"}]}
    return [("tool.started",{"name":name,"input":arguments,"call_id":call}),
            ("tool.completed",{"call_id":call,"output":json.dumps(output,ensure_ascii=False),"truncated":False})]


def approval(arguments):
    return SimpleNamespace(tool="mcp__team__delegate",input=arguments,status="allowed",consumed_at=object())


ARGS={"agent_ids":[7],"task_id":6,"mode":"work","brief":"测试工单","done_when":"测试验收"}


def test_selects_real_approved_call_and_rejects_unmatched_effects():
    second={k:v for k,v in ARGS.items() if k!="task_id"}
    evidence=tool_pair(ARGS)+tool_pair(second,call="two")
    assert recovery.failed_call(evidence,[approval(ARGS),approval(second)],6,7)==ARGS
    assert recovery.failed_call(tool_pair(ARGS,code="assignment_reference_scope"),[approval(ARGS)],6,7)==ARGS
    for bad, approvals in (
        (evidence[:-1],[approval(ARGS),approval(second)]),
        (evidence+[("tool.completed",{"call_id":"third"})],[approval(ARGS),approval(second)]),
        (tool_pair(ARGS,code="unknown"),[approval(ARGS)]),
        (tool_pair(ARGS,name="mcp__team__create_agent"),[approval(ARGS)]),
        (tool_pair({**ARGS,"agent_ids":[7,9]}),[approval({**ARGS,"agent_ids":[7,9]})]),
        (tool_pair(ARGS),[]),
        (tool_pair(second),[approval(second)]),
    ):
        with pytest.raises(recovery.RecoveryDenied):
            recovery.failed_call(bad,approvals,6,7)


@pytest.fixture
async def setup_case(sessions,owner,tmp_path,monkeypatch):
    from app.feishu.worker import Worker
    monkeypatch.setattr(settings,"STEWARD_STRICT_OWNER",owner)
    cfg={"owner_id":owner,"_access_config_path":str(tmp_path/"channel.json"),"state_dir":"channel","project":"default",
         "steward":{"app_id":"test-app","open_id":"test-owner","tenant_key":"tenant"},"roles":{}}
    (tmp_path/"channel.json").write_text("{}")
    (tmp_path/"channel").mkdir()
    monkeypatch.setattr(feishu_assignment,"configuration",lambda:cfg)
    async with sessions() as db:
        lead=await agent_store.create_agent(db,owner_id=owner,name="Supervisor",provider="codex",model="synthetic",make_lead=True)
        child=await agent_store.create_agent(db,owner_id=owner,name="Archivist",provider="codex")
        await service.change_member(db,owner,child.id)
        lead.delegation_enabled=True
        child.role_path=str(tmp_path/"employee")
        (tmp_path/"employee").mkdir()
        (tmp_path/"employee"/"AGENTS.md").write_text("---\ndefault_model: synthetic\n---\n")
        cfg["steward"]["agent_id"]=lead.id
        chat=await thread_store.create_thread(db,owner_id=owner,provider="codex",model="synthetic",agent_id=lead.id,
                                              workspace_path=str(tmp_path))
        cfg["steward_thread"]=chat.id
        db.add(FeishuLeadChat(thread_id=chat.id,channel=recovery.signature(owner,"test-app","tenant","test-owner","default",chat.id),
                              selection=recovery.signature("codex",lead.model),model=lead.model,project="default",isolated=False))
        await db.commit()
        result=await feature_tasks.route(db,owner_id=owner,agent_id=child.id,project="other-project",event_key="prior",
            title="Synthetic card",text="Synthetic assignment",intent="independent")
        task=result["task_id"]
        first=await thread_store.start_turn(db,chat.id,"Initial request")
        await thread_store.finish_turn(db,first.id,first.owner_token,status="succeeded",result="Ready")
        prior=AgentJob(owner_id=owner,kind="job",reply="resume",status="completed",origin_kind="person",task_id=task,
            requester_agent_id=lead.id,requester_thread_id=chat.id,requester_turn_id=first.id,
            assignee_agent_id=child.id,project="other-project",assignment_epoch=1,mode="work",brief="prior",brief_hash="prior")
        db.add(prior)
        await db.commit()
        original=f"续办工单 #{task}，让Archivist处理测试资料。"
        key=recovery.event_key({"app":"test-app","event":"test-event"})
        turn=await thread_store.start_turn(db,chat.id,original,direct_event_key=key,origin="person")
        args={**ARGS,"agent_ids":[child.id],"task_id":task}
        events=tool_pair(args)
        for kind,data in events:
            await thread_store._add_event(db,chat.id,kind,data,turn.id)
        db.add(Approval(id=f"test-{turn.id}",request_id="req",thread_id=chat.id,turn_id=turn.id,tool=recovery.TOOL,
                        input=args,status="allowed",decided_at=service.now(),delivered_at=service.now(),consumed_at=service.now()))
        await db.commit()
        await thread_store.finish_turn(db,turn.id,turn.owner_token,status="succeeded",result="派工被拒")
    with closing(routing.journal(tmp_path/"channel"/"channel.sqlite3")) as journal:
        worker=Worker(cfg,SimpleNamespace(db=journal))
        worker.api.close()
        msg={"chat_id":"test-chat","chat_type":"p2p","message_type":"text","message_id":"test-message",
             "content":json.dumps({"text":original},ensure_ascii=False)}
        with journal:
            journal.execute("insert into inbox(app,event,message,payload,state) values(?,?,?,?,?)",
                        ("test-app","test-event","test-message",json.dumps(msg),"done"))
            journal.execute("insert into owner_dm_sources values(?,?,?,?,?,?)",
                            ("test-app","test-event","test-message","test-owner","tenant","test-chat"))
            journal.execute("insert into direct_turns values(?,?,?,?)",("test-app","test-event",chat.id,turn.id))
            journal.execute("insert into direct_requests values(?,?,?,?)",("test-app","test-event",chat.id,
                            json.dumps({"prompt_text":original,"attachment_ids":[],"direct_event_key":key})))
            journal.execute("insert into outbox(id,role,body,state,remote) values('old-final','steward',?,'sent','old-remote')",
                            (json.dumps({"reply_to":"test-message"}),))
            journal.execute("insert into direct_finals values('test-app','test-event','old-final')")
    return cfg,chat.id,turn.id,task,child.id,args


async def test_operator_registers_once_binds_separate_system_turn_and_original_reply(setup_case,sessions,monkeypatch):
    cfg,thread,old_turn,task,employee,args=setup_case
    assert (await recovery.recover(sessions,cfg,old_turn,task))=={"eligible":True,"task":task,"employee":employee}
    with recovery.channel(cfg) as journal:
        assert journal.execute("select count(*) from direct_team_reports").fetchone()[0]==0
    registered=await recovery.recover(sessions,cfg,old_turn,task,apply=True)
    again=await recovery.recover(sessions,cfg,old_turn,task,apply=True)
    assert registered["state"]=="resume" and registered["root"]==again["root"] and again["already_registered"]
    with recovery.channel(cfg) as journal:
        row=journal.execute("select i.state,i.message,r.job from inbox i join direct_team_reports r using(app,event)").fetchone()
        assert tuple(row)==("done","test-message",registered["root"])
    from app.services.thread_runtime import ThreadRuntime
    from app.services.thread_hub import ThreadHub
    from app.services.provider_adapter import Capabilities,TurnOutcome
    from tests.team.test_collaboration import Engine
    from app.db import session
    from app.feishu import lead_chat
    from app.feishu.worker import Worker
    from app.services import tool_policy
    from app.team.bridge import TeamBridge
    engine=Engine()
    engine.key="codex"
    engine.capabilities=Capabilities(tools=True,approvals=True,mcp=True,project_workspace=True,resume=True)
    rt=ThreadRuntime(hub=ThreadHub(),adapters={"codex":engine},_session_factory=sessions,owner_id=cfg["owner_id"])
    observed=[]
    children=[]
    monkeypatch.setattr(rt.team_jobs,"kick",lambda:None)
    async def run(ctx,sink,gate):
        active=rt._runs[ctx.turn_id]
        if active.recovery_call is None:
            return TurnOutcome("succeeded",result="系统已收到员工交付")
        assert active.recovery_call==args and "Synthetic" not in recovery.PROMPT
        altered={**args,"brief":"different model wording","done_when":"different wording"}
        assert recovery.canonical_call(args,recovery.TOOL,altered)==args
        assert tool_policy.automatic("Bash","full_access",active,{"command":"echo"})[0]=="denied"
        async with sessions() as db:
            root=await db.get(AgentJob,registered["root"])
            assert root.current_turn_id==ctx.turn_id and root.requester_turn_id==old_turn
            assert (await recovery.checked_wake(db,root)).arguments==args
            actual={k:v for k,v in args.items() if k!="agent_ids"}
            assert await recovery.guard_child(db,root,{**actual,"agent_id":employee},employee,"resume") is None
        handle=await gate.open(request_id="new-approval",tool=recovery.TOOL,input=altered,summary="Synthetic",kind="action")
        assert (await gate.decide(handle)).kind=="allow"
        bridge=TeamBridge(rt,active)
        try:
            request=bridge.folder/"req-synthetic.working"
            request.write_text(json.dumps({"name":"delegate","arguments":altered}),encoding="utf-8")
            await bridge.handle(request)
            answer=json.loads((bridge.folder/"resp-synthetic.json").read_text(encoding="utf-8"))
            assert "error" not in answer, answer
            children.append(answer["receipts"][0]["job_id"])
        finally:
            await bridge.close()
        observed.append(ctx.turn_id)
        return TurnOutcome("succeeded",result="系统续办已核对")
    engine.handlers[thread]=run
    monkeypatch.setattr(session,"AsyncSessionLocal",sessions)
    from app import main
    from app.core.scheduler_lock import SchedulerLock
    from app.services import thread_runtime
    async def idle():
        return None
    monkeypatch.setattr(thread_runtime,"runtime",rt)
    monkeypatch.setattr(main.routine_service.scheduler,"recover",idle)
    monkeypatch.setattr(main.routine_service.scheduler,"tick",idle)
    monkeypatch.setattr(rt.rooms,"tick",idle)
    monkeypatch.setattr(rt.notices,"tick",idle)
    monkeypatch.setattr(rt.team_jobs,"dispatch_queued",idle)
    background=main._GuardedRoutines(SimpleNamespace(state=SimpleNamespace()),SchedulerLock("sqlite"))
    try:
        await background.tick()
        from tests.services.thread_harness import wait_until
        async def first_result():
            async with sessions() as db:
                root=await db.get(AgentJob,registered["root"])
                completed=await db.get(Turn,root.assignee_turn_id) if root.assignee_turn_id else None
                return completed if completed and completed.status!="running" else None
        await wait_until(first_result)
        assert observed and children
        if observed[0] in rt._runs:
            await rt._runs[observed[0]].task
        await rt.team_jobs._finish_execution(registered["root"])
        async with sessions() as db:
            original=await db.get(Turn,old_turn)
            assert original.status=="succeeded" and original.result=="派工被拒"
            started=await db.scalar(select(Event.data).where(Event.turn_id==observed[0],Event.type=="turn.started"))
            assert started["internal"] and started["team_generated"] and started["prompt_text"]==recovery.PROMPT
            child=await db.get(AgentJob,children[0])
            assert (child.task_id,child.assignee_agent_id,child.brief)==(task,employee,args["brief"])
            assert child.origin_kind=="person" and child.requester_turn_id==old_turn
            assert (await db.get(Turn,observed[0])).origin=="harness"
            fresh=await db.scalar(select(Approval).where(Approval.turn_id==observed[0]))
            assert fresh and fresh.consumed_at and fresh.input==args
            from app.db.models import AgentJobEvent
            owner_request=await db.scalar(select(AgentJobEvent.detail).where(AgentJobEvent.job_id==child.id,
                                                                            AgentJobEvent.kind=="owner_request"))
            assert (owner_request["turn_id"],owner_request["thread_id"])==(old_turn,thread)
        await rt.team_jobs.settle(children[0],"completed","done",result="Synthetic employee receipt")
        await rt.team_jobs.promote()
        await rt.team_jobs.dispatch_resumes()
        async def report_finished():
            async with sessions() as db:
                return (await db.get(AgentJob,registered["root"])).status=="completed"
        await wait_until(report_finished)
        assert len([ctx for ctx in engine.contexts if ctx.thread_id==thread])==1
        with recovery.channel(cfg,writable=True) as journal:
            worker=Worker(cfg,SimpleNamespace(db=journal))
            try:
                await lead_chat.publish_reports(worker)
                await lead_chat.publish_reports(worker)
                deliveries=journal.execute("select json_extract(body,'$.reply_to') from outbox where id!='old-final'").fetchall()
                assert [r[0] for r in deliveries]==["test-message"]
            finally:
                worker.api.close()
    finally:
        await rt.shutdown()


@pytest.mark.parametrize("change",["identity","digest","new_owner_request","prior_side_effect","epoch","employee","lead_model"])
async def test_rejects_stale_or_untrusted_before_registering(setup_case,sessions,change):
    cfg,thread,turn,task,employee,_=setup_case
    if change=="identity":
        cfg={**cfg,"steward":{**cfg["steward"],"tenant_key":"different"}}
    elif change=="digest":
        with recovery.channel(cfg,writable=True) as db, db:
            db.execute("update direct_requests set body=json_set(body,'$.skills',json('[1]'))")
    elif change=="new_owner_request":
        async with sessions() as db:
            new=await thread_store.start_turn(db,thread,"newer owner instruction")
            await thread_store.finish_turn(db,new.id,new.owner_token,status="succeeded")
    else:
        from app.db.models import Agent, WorkItem, FeatureScope
        async with sessions() as db:
            if change=="epoch":
                (await db.get(FeatureScope,task)).assignment_epoch=2
            elif change=="employee":
                (await db.get(WorkItem,task)).agent_id=cfg["steward"]["agent_id"]
            elif change=="lead_model":
                (await db.get(Agent,cfg["steward"]["agent_id"])).model="other-model"
            else:
                await thread_store._add_event(db,thread,"tool.started",{"name":"Bash","call_id":"unknown","input":{}},turn)
            await db.commit()
    with pytest.raises(ValueError):
        await recovery.recover(sessions,cfg,turn,task,apply=True)
    with recovery.channel(cfg) as journal:
        assert journal.execute("select count(*) from direct_team_reports").fetchone()[0]==0


@pytest.mark.parametrize("child_state",["no_child","failed","completed_without_receipt","superseded","confirmed"])
async def test_recovery_success_depends_on_current_child_and_its_publication(setup_case,sessions,child_state):
    from app.db.models import AgentJobEvent
    from app.team.jobs import Jobs
    cfg,thread,old_turn,task,employee,args=setup_case
    queued=await recovery.recover(sessions,cfg,old_turn,task,apply=True,require_document=True)
    async with sessions() as db:
        root=await db.get(AgentJob,queued["root"])
        new=await thread_store.start_turn(db,thread,recovery.PROMPT,internal=True)
        root.current_turn_id,root.assignee_turn_id,root.status=new.id,new.id,"running"
        if child_state!="no_child":
            child=AgentJob(owner_id=cfg["owner_id"],parent_id=root.id,root_id=root.id,kind="job",reply="resume",
                requester_thread_id=thread,requester_turn_id=old_turn,requester_agent_id=cfg["steward"]["agent_id"],
                task_id=task,assignee_agent_id=employee,assignment_epoch=1,project="other-project",origin_kind="person",
                brief="test",brief_hash="hash",mode="work",status="failed" if child_state=="failed" else "completed",
                result="模型自报已成功")
            db.add(child)
            await db.flush()
            key=recovery.event_key({"app":"test-app","event":"test-event"})
            db.add(AgentJobEvent(job_id=child.id,kind="owner_request",detail={"turn_id":old_turn,"thread_id":thread,"event_key":key}))
            if child_state in ("confirmed","superseded"):
                db.add(AgentJobEvent(job_id=child.id,kind="feishu_delivered",detail={"employee":employee,"epoch":1,
                    "tool":"feishu_publish_knowledge","result":{"url":"https://example.feishu.cn/docx/Test","messages_read":4}}))
                if child_state=="superseded":
                    db.add(AgentJobEvent(job_id=child.id,kind="feishu_superseded",detail={"employee":employee,
                        "epoch":1,"tool":"feishu_publish_knowledge"}))
        await db.commit()
        await thread_store.finish_turn(db,new.id,new.owner_token,status="succeeded",result="全部办好了。")
    jobs=Jobs(SimpleNamespace(session=sessions,owner_id=cfg["owner_id"],hub=SimpleNamespace(notify=lambda *_:None)))
    jobs.kick=lambda:None
    await jobs._finish_execution(queued["root"])
    async with sessions() as db:
        root=await db.get(AgentJob,queued["root"])
        assert root.status==("completed" if child_state=="confirmed" else "failed")
        assert "全部办好了" not in (root.result or "") and "模型自报" not in (root.result or "")
        if child_state=="confirmed":
            assert "/docx/Test" in root.result and "4" in root.result and "Archivist" in root.result
