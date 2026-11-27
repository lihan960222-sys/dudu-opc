from types import SimpleNamespace as NS
import pytest
from sqlalchemy import select
from app.db.models import AgentJob, Turn
from app.services import agent_store, feature_tasks, thread_store
from app.team import service
from tests.services.thread_harness import owner, sessions
from app.team.bridge import TeamBridge
from app.team.jobs import Jobs
from app.team.policy import TurnTeam, automatic


@pytest.fixture
def scoped_codex_host(tmp_path, monkeypatch):
    from app.services import employee_runtime

    # These scenarios exercise the scoped, ephemeral provider. Native project
    # resume, persistent storage and sandboxing are covered in test_codex_adapter.
    monkeypatch.setattr(employee_runtime, "native_enabled", lambda _owner, _agent: False)
    # These scenarios have no Feishu account. A developer's real gateway
    # binding and home snapshots must not affect their synthetic work orders.
    monkeypatch.setenv("FEISHU_CONFIG", str(tmp_path / "no-feishu.json"))
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))

@pytest.mark.asyncio
@pytest.mark.parametrize("transferred", [False, True])
async def test_employee_project_consultation(sessions, owner, tmp_path, transferred):
    async with sessions() as db:
        people = []
        for name in ("lead","engineer","analyst","outsider"):
            person = await agent_store.create_agent(db, owner_id=owner, name=name, provider="codex")
            folder = tmp_path / name
            folder.mkdir()
            person.role_path = str(folder)
            people.append(person)
        lead, engineer, analyst, outsider = people
        team = await service.set_lead(db,owner,lead.id)
        for agent in people[1:]:
            await service.change_member(db,owner,agent.id)
        for member, project in [(analyst,"p"),(outsider,"q"),(engineer,"p")]:
            result = await feature_tasks.route(db,owner_id=owner,agent_id=member.id,project=project,
                title="Review",text="review",event_key=member.name)
        task = result["task_id"]
        source_key = engineer.name
        if transferred:
            previous = await feature_tasks.route(db,owner_id=owner,agent_id=outsider.id,project="p",
                title="Moved review",text="review after handoff",event_key="former")
            task = previous["task_id"]
            await feature_tasks.reassign(db,task,1,engineer.id,"owner-correction",owner_id=owner)
            source_key = "new-employee"
            await feature_tasks.route(db,owner_id=owner,agent_id=engineer.id,project="p",title="Moved review",
                                      text="continue",event_key=source_key,reference_id=task)
        extra = await feature_tasks.route(db,owner_id=owner,agent_id=analyst.id,project="p",
            title="Second review",text="independent work",event_key="analyst-second",intent="independent")
        assert extra["action"] == "created"
        thread,_ = await feature_tasks.session_for(db,owner,task,model="m",workspace_path=str(tmp_path))
        parent = await feature_tasks.dispatch(db,owner_id=owner,steward_id=lead.id,task_id=task,
            event_key=source_key,thread_id=thread.id)
        turn = await thread_store.start_turn(db,thread.id,"review",feature_event_key=source_key)
    team_ctx = TurnTeam(team.id,engineer.id,False,False,strict_hierarchy=True,feature_consult=True)
    rt = NS(session=sessions,owner_id=owner,hub=NS(notify=lambda _:None))
    jobs = Jobs(rt)
    run = NS(turn_id=turn.id,thread_id=thread.id,agent_id=engineer.id,owner_token=turn.owner_token,
             ctx=NS(team=team_ctx,approval_mode="ask",feature_workspace=str(tmp_path)))
    bridge = TeamBridge(rt,run)
    try:
        roster = await bridge.execute("list_agents",{})
        assert [x["id"] for x in roster["agents"]] == [analyst.id]
        args = dict(agent_id=analyst.id,mode="consult",brief="What units apply to stress?",
                    done_when="Provide units with a reference.")
        assert automatic("mcp__team__ask_agent","ask",team_ctx,args)[0] == "allowed"
        for invalid in [dict(mode="work"),dict(agent_id=outsider.id),dict(agent_id=lead.id)]:
            with pytest.raises(service.TeamError):
                await jobs.enqueue(run,{**args,**invalid})
        with pytest.raises(service.TeamError):
            await jobs.enqueue(run,args,reply="resume")
        first = await jobs.enqueue(run,args)
        assert (first.parent_id,first.task_id,first.project,first.approval_mode)==(parent,task,"p","plan")
        assert first.assignment_epoch == (2 if transferred else 1)
        async with sessions() as db:
            assert await feature_tasks.assignment_current(db, first)
        assert (await jobs.enqueue(run,args)).id == first.id
        assert first.assignee_agent_id == analyst.id
        from fastapi import HTTPException
        from app.auth.policy import Principal
        from app.routers.team import job_detail, list_jobs
        device = NS(scope={"state":{"auth":Principal(kind="device",scopes=("chat",))}})
        assert await list_jobs(device,task_id=task,limit=10,rt=rt) == []
        with pytest.raises(HTTPException) as caught:
            await job_detail(first.id,device,rt)
        assert caught.value.status_code == 404

    finally:
        await bridge.close()



@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Scoped feature execution currently requires Windows")
@pytest.mark.asyncio
async def test_scoped_codex_asks_and_receives_peer_answer(crt, sessions, owner, codex, tmp_path, monkeypatch, scoped_codex_host):
    from app.core.config import settings
    from app.services.role_workspace import initialize
    from app.db.models import AgentJobEvent, Approval, Thread, WorkItem
    from tests.services.thread_harness import finished
    monkeypatch.setattr(settings, "TEAM_ENABLED", True)
    monkeypatch.setattr(settings, "STEWARD_STRICT_OWNER", owner)
    async with sessions() as db:
        roles = []
        for name in ("manager","developer","analyst"):
            a = await agent_store.create_agent(db,owner_id=owner,name=name,provider="codex",model="gpt-fake")
            root = tmp_path/name
            root.mkdir()
            initialize(root, "gpt-fake", "Review questions in your own specialty.")
            a.role_path = str(root)
            roles.append(a)
        lead, requester, target = roles
        await service.set_lead(db,owner,lead.id)
        for member in roles[1:]:
            await service.change_member(db,owner,member.id)
            response = await feature_tasks.route(db,owner_id=owner,agent_id=member.id,project="weld",
                event_key=member.name,title="part",text="review part",acceptance="check units",deliverables=["unit note"])
            if member.id==requester.id:
                task=response["task_id"]
        workspace=tmp_path/"developer"/"任务"/str(task)
        workspace.mkdir(parents=True)
        thread,_=await feature_tasks.session_for(db,owner,task,model="gpt-fake",workspace_path=str(workspace))
        parent=await feature_tasks.dispatch(db,owner_id=owner,steward_id=lead.id,task_id=task,event_key=requester.name,
                                           thread_id=thread.id)
        await db.commit()
    args={"agent_id":target.id,"mode":"consult","brief":"Which unit? Supplied sheet specifies Pa.","done_when":"Cite sheet."}
    codex.plan({"type":"team","tool":"ask_agent","arguments":args},{"type":"chat","text":"Pa, per supplied sheet."},
               user_mcp={"third_party":{"enabled":True}})
    run = await crt.send_turn(thread.id,"Check units.",feature_event_key=requester.name)
    await finished(sessions,thread.id)
    async with sessions() as db:
        consult=await db.scalar(select(AgentJob).where(AgentJob.parent_id==parent,AgentJob.mode=="consult"))
        assert consult is not None and consult.status=="completed", [(e.get("event"),e.get("content")) for e in codex.log() if e.get("event")=="mcp_result"]
        assert consult.result=="Pa, per supplied sheet."
        assert (await db.get(WorkItem,task)).agent_id==requester.id
        assert (await db.get(Thread,consult.assignee_thread_id)).kind=="feature_consult"
        assert (await db.scalar(select(Approval).where(Approval.turn_id==run.id))).consumed_at
        history=list(await db.scalars(select(AgentJobEvent.kind).where(AgentJobEvent.job_id==consult.id)))
        assert history==["enqueued","admitted","started","settled"]
        protocol=await feature_tasks.protocol(db,owner,task)
        assert protocol["deliverables"]==["unit note"] and protocol["acceptance_criteria"]==["check units"]
    parent_config,child_config=[r["params"]["config"] for r in codex.received("thread/start")]
    assert set(parent_config["mcp_servers"]["team"]["enabled_tools"])=={"list_agents","ask_agent","delegate","answer_job_question","propose_direct_report","employee_capabilities","enable_local_skill"}
    assert not any(v.get("enabled",True) for v in child_config["mcp_servers"].values())
    assert all(not v for v in child_config["features"].values())
    assert any("Pa, per supplied sheet." in e.get("content","") for e in codex.log() if e.get("event")=="mcp_result")


from tests.services.test_codex_adapter import crt, codex  # noqa: E402,F401


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows scoped Codex")
@pytest.mark.asyncio
async def test_lead_native_delegate_runs_scoped_work_and_resumes(crt,sessions,owner,codex,tmp_path,monkeypatch,scoped_codex_host):
    from app.core.config import settings
    from app.services.role_workspace import initialize
    from app.db.models import Thread
    from tests.services.thread_harness import finished
    monkeypatch.setattr(settings,"TEAM_ENABLED",True)
    monkeypatch.setattr(settings,"TEAM_LEAD_AUTO",True)
    monkeypatch.setattr(settings,"STEWARD_STRICT_OWNER",owner)
    async with sessions() as db:
        lead=await agent_store.create_agent(db,owner_id=owner,name="Manager",provider="codex",model="gpt-fake")
        worker=await agent_store.create_agent(db,owner_id=owner,name="Staff",provider="codex",model="gpt-fake")
        folder=tmp_path/"worker"
        folder.mkdir()
        initialize(folder,"gpt-fake","Perform assigned work")
        worker.role_path=str(folder)
        await service.set_lead(db,owner,lead.id)
        await service.change_member(db,owner,worker.id)
    codex.plan({"type":"team","tool":"delegate","arguments":{"agent_ids":[worker.id],"brief":"Inspect units.",
                "project":"weld","done_when":"Include units","deliverables":["validation note"],"priority":"high"}},
               {"type":"chat","text":"Pa."},{"type":"chat","text":"Reviewed Pa."})
    lead_thread=await crt.create_thread(provider="codex",agent_id=lead.id)
    await crt.send_turn(lead_thread.id,"Assign this units review.")
    await finished(sessions,lead_thread.id,count=2)
    async with sessions() as db:
        job=await db.scalar(select(AgentJob).where(AgentJob.owner_id==owner,AgentJob.kind=="job"))
        assert (job.project,job.status,job.result)==("weld","completed","Pa.")
        protocol=await feature_tasks.protocol(db,owner,job.task_id,owner_handoff=True)
        assert protocol["from"]==lead.id and protocol["to"]==worker.id
        assert (protocol["status"],protocol["priority"],protocol["deliverables"])==("review","high",["validation note"])
        assert protocol["native_handoff"] == {"mode": "workbench", "thread_id": job.assignee_thread_id}
        assert not (await feature_tasks.protocol(db,owner,job.task_id))["native_handoff"]
        assert (await db.get(Thread,job.assignee_thread_id)).kind=="feature"
        assert await feature_tasks.thread_handoff(db,owner,lead_thread.id) == {"mode": "workbench", "thread_id": lead_thread.id}
    first_job,task_id,worker_chat = job.id, job.task_id, job.assignee_thread_id
    followup = {"agent_ids":[worker.id],"brief":"Check the revised units.", "task_id":task_id,
                "project":"weld","done_when":"Check revised units"}
    codex.plan(*([{"type":"chat","text":"old"}]*3), {"type":"team","tool":"delegate","arguments":followup},
               {"type":"chat","text":"Rechecked Pa."},{"type":"chat","text":"Delivered check."})
    next_turn = await crt.send_turn(lead_thread.id,"Please recheck the same work.",direct_event_key="followup")
    await finished(sessions,lead_thread.id,count=4)
    assert (await crt.send_turn(lead_thread.id,"Please recheck the same work.",direct_event_key="followup")).id == next_turn.id
    async with sessions() as db:
        jobs = list(await db.scalars(select(AgentJob).where(AgentJob.task_id==task_id,AgentJob.kind=="job").order_by(AgentJob.id)))
        assert len(jobs)==2 and jobs[0].id == first_job
        assert jobs[0].assignee_turn_id != jobs[1].assignee_turn_id
        assert jobs[1].assignee_thread_id == worker_chat and jobs[1].result == "Rechecked Pa."
        assert await feature_tasks.thread_handoff(db,owner,worker_chat) == {"mode": "workbench", "thread_id": worker_chat}
    assert not codex.received("thread/resume")
    assert all(call["params"]["ephemeral"] for call in codex.received("thread/start"))


def test_native_metadata_requires_exact_checkpoint_and_strips_contents():
    from app.services.codex_adapter import _native_activity
    page = {"data":[{"id":"external","items":["private"]},{"id":"last-local"}]}
    assert _native_activity(page, "last-local") == {"source":"native_session","count":1,"latest_turn_id":"external"}
    assert _native_activity(page, "external") is None
    assert _native_activity(page, "missing") is None


@pytest.mark.asyncio
async def test_protocol_revision_and_dependency_graph(sessions,owner):
    async with sessions() as db:
        role = await agent_store.create_agent(db,owner_id=owner,name="planner",provider="codex")
        cards = []
        for title,project in [("one","p"),("two","p"),("three","q")]:
            row = await feature_tasks.route(db,owner_id=owner,agent_id=role.id,project=project,
                title=title,text=title,event_key=title)
            cards.append(row["task_id"])
        await db.commit()
        first,second,foreign = cards
        current = await feature_tasks.protocol(db,owner,first)
        changed = await feature_tasks.update_protocol(db,owner,first,current["revision"],
            {"dependencies":[second], "deliverables":["worksheet"], "acceptance_criteria":["signed","dated"]})
        assert changed["revision"] == current["revision"] + 1
        assert changed["acceptance_criteria"] == ["signed","dated"]
        with pytest.raises(service.TeamError,match="changed"):
            await feature_tasks.update_protocol(db,owner,first,current["revision"],{"priority":"low"})
        await db.rollback()
        other = await feature_tasks.protocol(db,owner,second)
        for dependency,reason in [(first,"cycle"),(foreign,"same project")]:
            with pytest.raises(ValueError,match=reason):
                await feature_tasks.update_protocol(db,owner,second,other["revision"],{"dependencies":[dependency]})
            await db.rollback()

@pytest.mark.asyncio
async def test_dependency_acceptance_starts_queue_clock(sessions,owner):
    from datetime import timedelta
    from app.db.models import WorkItem
    async with sessions() as db:
        agent = await agent_store.create_agent(db,owner_id=owner,name="worker",provider="codex")
        args = dict(db=db,owner_id=owner,agent_id=agent.id,project="project",text="work")
        before = (await feature_tasks.route(**args,title="first",event_key="first"))["task_id"]
        after = (await feature_tasks.route(**args,title="next",event_key="next",dependencies=[before]))["task_id"]
        now = service.now()
        old_job = NS(owner_id=owner,task_id=after,created_at=now-timedelta(days=4))
        assert await feature_tasks.eligible_since(db,old_job) is None
        (await db.get(WorkItem,before)).completed_at = now
        await db.commit()
    async with sessions() as reopened:
        assert await feature_tasks.eligible_since(reopened,old_job) == now

@pytest.mark.asyncio
async def test_direct_steward_event_is_atomic_and_checks_original_input(sessions,owner):
    import asyncio
    from sqlalchemy import func
    async with sessions() as db:
        lead = await agent_store.create_agent(db,owner_id=owner,name="lead",provider="codex")
        await service.set_lead(db,owner,lead.id)
        threads = [await thread_store.create_thread(db,owner_id=owner,provider="codex",agent_id=lead.id) for _ in range(2)]
    async def start(target,**changes):
        async with sessions() as db:
            inputs = dict(thread_id=target,prompt_text="Original",direct_event_key="event",
                          direct_attachment_ids=["a","b"],direct_skills=["humanizer-zh"])
            try:
                turn = await thread_store.start_turn(db,**{**inputs,**changes})
                return (turn.id,True)
            except thread_store.TurnAlreadyStarted as replay:
                return (replay.turn.id,False)
    first,second = await asyncio.gather(start(threads[0].id),start(threads[0].id))
    assert first[0] == second[0] and first[1] != second[1]
    for changes in [{"direct_skills":[]},{"prompt_text":"changed"},{"direct_attachment_ids":["b","a"]}]:
        with pytest.raises(feature_tasks.DispatchConflict):
            await start(threads[0].id,**changes)
    with pytest.raises(feature_tasks.DispatchConflict):
        await start(threads[1].id)
    async with sessions() as db:
        assert await db.scalar(select(func.count(Turn.id)).where(Turn.thread_id.in_([t.id for t in threads]))) == 1


@pytest.mark.asyncio
async def test_protocol_http_only_for_current_owner(sessions,owner):
    from fastapi import HTTPException
    from app.auth.policy import Principal, required_scope, allows
    from app.routers.team import task_protocol
    async with sessions() as db:
        a = await agent_store.create_agent(db,owner_id=owner,name="worker",provider="codex")
        task = (await feature_tasks.route(db,owner_id=owner,agent_id=a.id,project="internal",
                title="Proposal",text="owner material",event_key="private"))["task_id"]
        await db.commit()
    rt = NS(owner_id=owner,session=sessions)
    def request(principal):
        return NS(scope={"state":{"auth":principal}})
    scope = required_scope("GET",f"/api/v1/team/tasks/{task}/protocol")
    assert not allows(("chat",),scope)
    for principal in [Principal(kind="device",scopes=("chat",)), Principal(kind="device",scopes=("admin",)),
                      Principal(kind="browser",scopes=("admin",),role="member")]:
        with pytest.raises(HTTPException) as result:
            await task_protocol(task,request(principal),rt)
        assert result.value.status_code == 403
    record = await task_protocol(task,request(Principal(kind="browser",role="owner")),rt)
    assert record["objective"] == "owner material"


@pytest.mark.asyncio
async def test_employee_selects_bundled_skill_in_scoped_turn(sessions,owner,tmp_path,monkeypatch):
    from app.services import local_claude_runner,agent_skills
    monkeypatch.setattr(local_claude_runner,"workspace_dir",lambda:tmp_path)
    async with sessions() as db:
        boss=await agent_store.create_agent(db,owner_id=owner,name="boss",provider="codex")
        employee=await agent_store.create_agent(db,owner_id=owner,name="employee",provider="codex")
        team=await service.set_lead(db,owner,boss.id)
        await service.change_member(db,owner,employee.id)
        thread=await thread_store.create_thread(db,owner_id=owner,agent_id=employee.id,provider="codex")
        turn=await thread_store.start_turn(db,thread.id,"Use a suitable local skill")
    ctx=NS(team=TurnTeam(team.id,employee.id,False,False,feature_consult=True,delegation_enabled=False),
           approval_mode="ask",community_tools=["already_granted"],composio_launch=None,composio_exact_tools=None)
    run=NS(turn_id=turn.id,owner_token=turn.owner_token,agent_id=employee.id,ctx=ctx)
    bridge=TeamBridge(NS(session=sessions,owner_id=owner),run)
    try:
        catalog=await bridge.execute("employee_capabilities",{})
        assert catalog["granted_tools"]==["already_granted"]
        assert catalog["feishu_tool_details"]=={}
        from dataclasses import replace
        from unittest.mock import AsyncMock
        from app.team import feishu_tools_bridge
        ctx.team=replace(ctx.team,feishu_tools=("feishu_list_sources","feishu_publish_knowledge"))
        current=AsyncMock(return_value=NS(tools=ctx.team.feishu_tools))
        monkeypatch.setattr(feishu_tools_bridge,"grant",current)
        details=(await bridge.execute("employee_capabilities",{}))["feishu_tool_details"]
        assert set(details)==set(ctx.team.feishu_tools)
        assert "same-title" in details["feishu_list_sources"] and "returns the document link" in details["feishu_publish_knowledge"]
        current.return_value=NS(tools=("feishu_list_sources",))
        after_revocation=await bridge.execute("employee_capabilities",{})
        assert "feishu_publish_knowledge" not in after_revocation["granted_tools"]
        assert set(after_revocation["feishu_tool_details"])=={"feishu_list_sources"}
        skill=catalog["available"][0]
        chosen={k:skill[k] for k in ("template_id","name")}
        result=await bridge.execute("enable_local_skill",chosen)
        assert result["enabled"] and result["body"]
        assert (await bridge.execute("employee_capabilities",{}))["enabled"]==[chosen["name"]]
        with pytest.raises(service.TeamError):
            await bridge.execute("enable_local_skill",{**chosen,"agent_id":boss.id})
        assert not agent_skills.list_skills(boss.id)
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_steward_employee_modes_and_legacy_overlaps(sessions,owner,tmp_path,monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings,"STEWARD_STRICT_OWNER",owner)
    async with sessions() as db:
        boss=await agent_store.create_agent(db,owner_id=owner,name="boss",make_lead=True,approval_mode="full_access")
        member=await agent_store.create_agent(db,owner_id=owner,name="employee",approval_mode="full_access")
        assert boss.approval_mode=="full_access" and member.approval_mode=="accept_edits"
        assert (await agent_store.update_agent(db,member.id,approval_mode="approve_for_me")).approval_mode=="ask"
        member.approval_mode="full_access"
        await db.commit()
        await agent_store.cap_existing_employee_modes(db,owner)
        await db.refresh(member)
        await db.refresh(boss)
        assert member.approval_mode=="accept_edits" and boss.approval_mode=="full_access"
        assert (await agent_store.update_agent(db,member.id,title="updated")).approval_mode=="accept_edits"
        await service.set_lead(db,owner,member.id)
        assert boss.approval_mode=="accept_edits"
        root=tmp_path/"employee"
        boss.role_path=str(root/"nested")
        await db.commit()
        with pytest.raises(ValueError,match="重叠"):
            await feature_tasks.check_employee_root(db,owner,member,root)
        assert not root.exists()


@pytest.mark.skipif(__import__("sys").platform != "win32",reason="Windows scoped Codex")
@pytest.mark.asyncio
async def test_missing_scoped_file_reports_blocker_without_unrequested_retry(crt,sessions,owner,codex,tmp_path,monkeypatch,scoped_codex_host):
    from app.core.config import settings
    from app.services.role_workspace import initialize
    from app.db.models import AgentJobEvent
    from tests.services.thread_harness import wait_until
    monkeypatch.setattr(settings,"TEAM_ENABLED",True)
    monkeypatch.setattr(settings,"TEAM_LEAD_AUTO",True)
    monkeypatch.setattr(settings,"STEWARD_STRICT_OWNER",owner)
    async with sessions() as db:
        boss=await agent_store.create_agent(db,owner_id=owner,name="Manager",provider="codex",model="gpt-fake")
        employee=await agent_store.create_agent(db,owner_id=owner,name="Specialist",provider="codex",model="gpt-fake")
        role=tmp_path/"specialist"
        role.mkdir()
        initialize(role,"gpt-fake","Complete assignments")
        employee.role_path=str(role)
        await service.set_lead(db,owner,boss.id)
        await service.change_member(db,owner,employee.id)
    codex.plan({"type":"team","tool":"delegate","arguments":{"agent_ids":[employee.id],"brief":"Prepare file.",
               "project":"demo","done_when":"File delivered","completion_check":{"kind":"files",
               "files":[{"scope":"task","path":"output.md"}]}}},
               {"type":"chat","text":"File ready."},
               {"type":"chat","text":"File not confirmed."})
    lead_chat=await crt.create_thread(provider="codex",agent_id=boss.id)
    await crt.send_turn(lead_chat.id,"Have the specialist save output.md.")
    async def manager_received_report():
        async with sessions() as db:
            return await db.scalar(select(AgentJob.id).where(AgentJob.owner_id==owner,
                AgentJob.requester_thread_id==lead_chat.id,AgentJob.kind=="root",AgentJob.status=="completed"))
    await wait_until(manager_received_report, timeout=60)
    async with sessions() as db:
        job=await db.scalar(select(AgentJob).where(AgentJob.owner_id==owner,AgentJob.kind=="job"))
        assert (job.status,job.outcome,job.executions)==("failed","verification_blocked",1)
        assert "output.md" in job.outcome_detail
        events=list(await db.scalars(select(AgentJobEvent.kind).where(AgentJobEvent.job_id==job.id)))
        assert events.count("verification_retry")==0
        assert events.count("completion_evidence")==1
