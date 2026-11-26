import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.db.models import AgentJob, AgentJobEvent
from app.services import agent_store, feature_tasks, thread_store
from app.team import feishu_tools_bridge as bridge, service
from tests.services.thread_harness import owner, sessions  # noqa: F401


@pytest.mark.parametrize("final", [False, True], ids=["failure", "confirmed_remote_effect"])
async def test_receipt_refreshes_worker_read_snapshot_after_reassignment(sessions,owner,tmp_path,final):
    from contextlib import asynccontextmanager
    async with sessions() as db:
        lead = await agent_store.create_agent(db,owner_id=owner,name="Lead",make_lead=True)
        old = await agent_store.create_agent(db,owner_id=owner,name="First")
        new = await agent_store.create_agent(db,owner_id=owner,name="Second")
        for employee in (old,new):
            await service.change_member(db,owner,employee.id)
        card = await feature_tasks.route(db,owner_id=owner,agent_id=old.id,event_key="receipt",
                                        project="P",title="Document",text="Prepare the draft")
        thread, _ = await feature_tasks.session_for(db,owner,card["task_id"],model="m",workspace_path=str(tmp_path))
        job_id = await feature_tasks.dispatch(db,owner_id=owner,steward_id=lead.id,task_id=card["task_id"],
                                              event_key="receipt",thread_id=thread.id)
        turn = await thread_store.start_turn(db,thread.id,"prepare",feature_event_key="receipt")
        job = await db.get(AgentJob,job_id)
        job.status = "running"
        await db.commit()
    async with sessions() as worker:
        # The worker took this snapshot when it checked its grant. A correction
        # commits independently before the provider's receipt comes back.
        snapshot = await worker.get(AgentJob,job_id)
        assert snapshot.status == "running"
        async with sessions() as correction:
            await feature_tasks.reassign(correction,card["task_id"],1,new.id,"replace",owner_id=owner)
            await correction.commit()
        @asynccontextmanager
        async def existing_session():
            yield worker
        rt = SimpleNamespace(owner_id=owner,session=existing_session)
        run = SimpleNamespace(agent_id=old.id,turn_id=turn.id)
        tool = "feishu_create_document"
        if final:
            with pytest.raises(service.TeamError) as denied:
                await bridge.record_delivery(rt,job_id,run,tool,tool,"create_document","operation",{"url":"https://example.org/original"})
            assert denied.value.code == "assignment_replaced"
        else:
            await bridge.record(rt,job_id,run,tool,"feishu_tool_failed",category="ReadTimeout")
        assert snapshot.outcome == "reassigned"
    async with sessions() as db:
        stages = list(await db.scalars(select(AgentJobEvent.kind).where(AgentJobEvent.job_id==job_id)))
        assert stages.count("feishu_superseded" if final else "feishu_tool_failed") == 1
        assert "feishu_delivered" not in stages


async def test_unknown_old_write_blocks_new_write_but_allows_authorized_read(sessions,owner,tmp_path,monkeypatch):
    tool = "feishu_create_document"
    read = "feishu_read_document"
    async with sessions() as db:
        lead = await agent_store.create_agent(db,owner_id=owner,name="Lead",make_lead=True)
        old = await agent_store.create_agent(db,owner_id=owner,name="First")
        new = await agent_store.create_agent(db,owner_id=owner,name="Next")
        for employee in (old,new):
            await service.change_member(db,owner,employee.id)
        card = await feature_tasks.route(db,owner_id=owner,agent_id=old.id,event_key="original",
            project="P",title="Document",text="Prepare the draft")
        task_id = card["task_id"]
        thread, _ = await feature_tasks.session_for(db,owner,task_id,model="m",workspace_path=str(tmp_path))
        first = await feature_tasks.dispatch(db,owner_id=owner,steward_id=lead.id,task_id=task_id,
                                           event_key="original",thread_id=thread.id)
        db.add(AgentJobEvent(job_id=first,kind="feishu_assignment",detail={"employee":old.id,
            "action":"create_document","event_key":"original","title":"First document"}))
        for kind, detail in [("feishu_tool_started",{}),("feishu_tool_failed",{"category":"ReadTimeout"})]:
            db.add(AgentJobEvent(job_id=first,kind=kind,detail={"tool":tool,"employee":old.id,"operation":"first-op",**detail}))
        await db.commit()
        await feature_tasks.reassign(db,task_id,1,new.id,"correction",owner_id=owner)
        await feature_tasks.route(db,owner_id=owner,agent_id=new.id,event_key="next",project="P",
                                  title="Use existing results",text="Check the draft",reference_id=task_id)
        thread, _ = await feature_tasks.session_for(db,owner,task_id,model="m",workspace_path=str(tmp_path))
        next_job = await feature_tasks.dispatch(db,owner_id=owner,steward_id=lead.id,task_id=task_id,
                                              event_key="next",thread_id=thread.id)
        turn = await thread_store.start_turn(db,thread.id,"check",feature_event_key="next")
        current = await db.get(AgentJob,next_job)
        current.origin_kind, current.status = "person", "running"
        db.add(AgentJobEvent(job_id=next_job,kind="feishu_assignment",
                             detail={"action":"create_document","employee":new.id,"event_key":"next"}))
        team = await service.team_for(db,owner)
        run = SimpleNamespace(agent_id=new.id,thread_id=thread.id,turn_id=turn.id,owner_token=turn.owner_token,
            ctx=SimpleNamespace(approval_mode="ask",team=SimpleNamespace(team_id=team.id,feishu_tools=(tool,read))))
        await db.commit()
    journal = tmp_path/"channel.sqlite3"
    sqlite3.connect(journal).close()
    cfg = {"_access_config_path":str(tmp_path/"cfg.json"),"state_dir":".","owner_id":owner,
           "steward":{"agent_id":lead.id,"app_id":"lead"}}
    from app.team import feishu_assignment
    monkeypatch.setattr(feishu_assignment,"configuration",lambda:cfg)
    pins = {"next":{"action":"create_document","employee":new.id,"event_key":"next"},
        "separate":{"action":"create_document","employee":new.id,"event_key":"separate","title":"Second document"}}
    monkeypatch.setattr(feishu_assignment,"gateway_pin",lambda cfg,key:pins.get(key))
    monkeypatch.setattr(bridge,"configured",lambda cfg,employee,proof:(proof,(tool,read)) if employee==new.id else None)
    monkeypatch.setattr(bridge.owner_oauth,"employee_config",lambda cfg,employee:{
        "_employee_oauth":True,"steward":{"app_id":f"employee-{employee}"}})
    gateway = AsyncMock(return_value={"complete":True,"url":"https://example.org/current"})
    monkeypatch.setattr(bridge.employee_tools,"execute",gateway)
    rt = SimpleNamespace(owner_id=owner,session=sessions)
    for attempt in range(2):
        with pytest.raises(service.TeamError) as error:
            await bridge.execute(rt,run,tool,{})
        assert error.value.code == "previous_write_unknown"
        if not attempt:
            assert (await bridge.execute(rt,run,read,{}))["complete"]
    assert [call.args[2] for call in gateway.await_args_list] == [read]
    async with sessions() as db:
        entries = list(await db.scalars(select(AgentJobEvent).where(AgentJobEvent.job_id==next_job,
                                     AgentJobEvent.kind=="feishu_tool_started")))
        assert [row.detail["tool"] for row in entries] == [read]
    with sqlite3.connect(journal) as ledger:
        ledger.execute("create table employee_document_jobs(app text,employee integer,job text,doc text,confirmed integer)")
        ledger.executemany("insert into employee_document_jobs values(?,?,?,?,?)",[
             ("wrong-app",old.id,"first-op","SECRET",1),(f"employee-{old.id}",new.id,"first-op","SECRET",1)])
    with pytest.raises(service.TeamError,match="核对"):
        await bridge.execute(rt,run,tool,{})
    with sqlite3.connect(journal) as ledger:
        ledger.execute("insert into employee_document_jobs values(?,?,?,?,1)",(f"employee-{old.id}",old.id,"first-op","SECRET"))
    gateway.return_value={"confirmed":True,"url":"https://example.org/current"}
    for _ in range(2):
        with pytest.raises(service.TeamError) as duplicate:
            await bridge.execute(rt,run,tool,{})
        assert duplicate.value.code == "previous_write_confirmed"
    assert [call.args[2] for call in gateway.await_args_list] == [read]
    async with sessions() as db:
        await thread_store.finish_turn(db,run.turn_id,run.owner_token,status="succeeded")
        await feature_tasks.route(db,owner_id=owner,agent_id=new.id,event_key="separate",project="P",
                                  title="Separately named document",text="Create the second document",reference_id=task_id)
        extra = await feature_tasks.dispatch(db,owner_id=owner,steward_id=lead.id,task_id=task_id,
                                           event_key="separate",thread_id=run.thread_id)
        t = await thread_store.start_turn(db,run.thread_id,"Create the separately named document",feature_event_key="separate")
        job = await db.get(AgentJob,extra)
        job.origin_kind, job.status = "person", "running"
        db.add(AgentJobEvent(job_id=extra,kind="feishu_assignment",detail=pins["separate"]))
        run.turn_id, run.owner_token = t.id, t.owner_token
        await db.commit()
    assert (await bridge.execute(rt,run,tool,{}))["confirmed"]
    assert [call.args[2] for call in gateway.await_args_list] == [read,tool]
    assert "SECRET" not in str(gateway.await_args)
    async with sessions() as db:
        entries = list(await db.scalars(select(AgentJobEvent.detail).where(AgentJobEvent.job_id==next_job)))
        assert "SECRET" not in str(entries)
