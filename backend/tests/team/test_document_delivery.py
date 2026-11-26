"""The ordinary owner-facing final answer must agree with the employee's current receipt."""
import pytest
from sqlalchemy import select

from app.db.models import AgentJob, AgentJobEvent, Event
from app.services.provider_adapter import TurnOutcome
from tests.services.thread_harness import cleanup, owner, sessions, wait_until  # noqa: F401
from tests.team.test_delegation import root_of, team, turns_of  # noqa: F401


URL = "https://company.feishu.cn/docx/Original"
INVENTED = "https://office.feishu.cn/docx/Invented"


@pytest.mark.parametrize("case", [
    "confirmed", "missing", "new_write_started", "readback_failed", "wrong_employee", "wrong_epoch",
    "other_turn", "different_target", "unrelated_read", "read_only", "ordinary",
])
async def test_normal_delegation_returns_only_the_current_document_delivery(team, sessions, case):
    rt, engine, lead, employee, _ = team
    chat = await rt.create_thread(agent_id=lead.id)
    tool = "feishu_read_document" if case=="read_only" else "feishu_edit_document"
    draft = "保留这条调研结论。" if case in ("read_only", "ordinary") else "所有工作都已完成。\n"+INVENTED
    calls = []

    async def worker(ctx):
        async def claimed():
            async with sessions() as db:
                return await db.scalar(select(AgentJob.id).where(AgentJob.assignee_turn_id==ctx.turn_id,
                                                                 AgentJob.kind=="job"))
        job_id = await wait_until(claimed)
        if case != "ordinary":
            async with sessions() as db:
                from app.db.transactions import begin_write
                await begin_write(db)
                job = await db.get(AgentJob, job_id)
                def event(kind, detail):
                    db.add(AgentJobEvent(job_id=job.id, kind=kind, detail=detail))
                event("feishu_assignment", {"action":tool.removeprefix("feishu_"), "employee":employee.id, "target_url":URL})
                stamp = {"tool":tool, "employee":employee.id, "turn_id":ctx.turn_id, "epoch":job.assignment_epoch}
                if case!="missing":
                    current = dict(stamp)
                    if case=="other_turn":
                        current["turn_id"] += 100
                    if case=="wrong_epoch":
                        current["epoch"] += 1
                    if case=="wrong_employee":
                        current["employee"] = lead.id
                    event("feishu_delivered", {**current, "result":{"url":INVENTED if case=="different_target" else URL}})
                if case=="new_write_started":
                    event("feishu_tool_started", stamp)
                if case=="readback_failed":
                    event("feishu_tool_failed", {**stamp, "tool":"feishu_read_document", "detail":"飞书拒绝读回正文。"})
                if case=="unrelated_read":
                    import hashlib
                    def key(url):
                        return hashlib.sha256(url.encode()).hexdigest()
                    read={**stamp,"tool":"feishu_read_document"}
                    event("feishu_tool_failed",{**read,"target_hash":key(URL)})
                    event("feishu_read_verified",{**read,"target_hash":key(INVENTED)})
                await db.commit()
        return TurnOutcome("succeeded", result="员工报告。")

    async def steward(ctx, sink, gate):
        calls.append(ctx.turn_id)
        if len(calls)==1:
            await rt.team_jobs.delegate(rt._runs[ctx.turn_id], {"agent_ids":[employee.id], "brief":"NESTED"})
            return TurnOutcome("succeeded", result="已登记。")
        sink.delta(draft)
        await sink.emit("assistant.message", {"text":draft})
        return TurnOutcome("succeeded", result=draft)

    engine.nested, engine.handlers[chat.id] = worker, steward
    await rt.send_turn(chat.id, "Please check the assigned deliverable")
    async def terminal():
        root=await root_of(sessions,chat.id)
        return root if root and root.status in ("completed","failed","cancelled") else None
    root = await wait_until(terminal)
    turn = (await turns_of(sessions,chat.id))[-1]
    async with sessions() as db:
        messages = list(await db.scalars(select(Event.data).where(Event.turn_id==turn.id,
            Event.type=="assistant.message").order_by(Event.id)))
    report = root.result if root.status=="completed" else root.outcome_detail
    assert [m["text"] for m in messages] == [turn.result] and report == turn.result
    if case!="ordinary":
        assert root.origin_kind=="person" and root.requester_turn_id==calls[0] and turn.origin=="harness"
    if case in ("ordinary","read_only"):
        assert root.status=="completed" and report==draft
    elif case=="confirmed":
        assert root.status=="completed" and URL in report and INVENTED not in report
    else:
        assert root.status=="failed" and root.outcome=="missing_delivery"
        assert INVENTED not in report and URL not in report and "所有工作都已完成" not in report
        assert "交付" in report


@pytest.mark.parametrize("choice", ["none", "create_document", "local_files", None])
async def test_new_owner_dispatch_selects_delivery_before_asking_for_approval(team, monkeypatch, choice):
    from app.core.config import settings
    from app.team import feishu_assignment
    from app.db.models import Approval
    rt,engine,lead,employee,_ = team
    monkeypatch.setattr(settings,"STEWARD_STRICT_OWNER",rt.owner_id)
    cfg={"owner_id":rt.owner_id, "steward":{"agent_id":lead.id,"app_id":"lead"},
         "roles":{"staff":{"agent_id":employee.id,"app_id":"employee"}}}
    monkeypatch.setattr(feishu_assignment,"configuration",lambda:cfg)
    monkeypatch.setattr(feishu_assignment.workspace_access,"settings_for",lambda _:{"history_sources":{}})
    chat=await rt.create_thread(agent_id=lead.id)
    observed = []

    async def steward(ctx, sink, gate):
        args={"agent_ids":[employee.id],"brief":"Collect the material"}
        if choice=="local_files":
            args["completion_check"]={"kind":"files","files":[{"scope":"task","path":"report.md"}]}
        elif choice:
            args["feishu_delivery"]=choice
        handle=await gate.open(request_id="one-call",tool="mcp__team__delegate",input=args,summary="delegate",kind="tool")
        async with rt.session() as db:
            approval=await db.get(Approval,handle.approval_id)
            observed.append(approval.status)
        return TurnOutcome("succeeded",result="model says success")

    engine.handlers[chat.id]=steward
    await rt.send_turn(chat.id,"Use the configured employee.")
    await wait_until(lambda:observed)
    assert (observed[0]=="denied") == (choice is None)


async def test_owner_does_not_receive_invented_success_after_unroutable_explicit_request(team, sessions):
    rt,engine,lead,employee,_ = team
    chat=await rt.create_thread(agent_id=lead.id)
    async def steward(ctx,sink,gate):
        response=await rt.team_jobs.delegate(rt._runs[ctx.turn_id],{"agent_ids":[employee.id],"brief":"deliver report",
                                             "feishu_delivery":"create_document"})
        assert response["code"]=="outside_scope"
        await sink.emit("assistant.message",{"text":INVENTED})
        return TurnOutcome("succeeded",result=INVENTED)
    engine.handlers[chat.id]=steward
    await rt.send_turn(chat.id,"Create a cloud report.")
    await wait_until(lambda:not rt._runs)
    final=(await turns_of(sessions,chat.id))[-1].result
    assert final.startswith("老板，") and "还没登记" in final and INVENTED not in final


async def test_failed_document_dispatch_survives_another_employees_success_and_supervisor_resume(team,sessions):
    rt,engine,lead,writer,analyst=team
    chat=await rt.create_thread(agent_id=lead.id)
    calls=[]
    async def steward(ctx,sink,gate):
        calls.append(ctx.turn_id)
        if len(calls)==1:
            run=rt._runs[ctx.turn_id]
            refused=await rt.team_jobs.delegate(run,{"agent_ids":[writer.id],"brief":"Write","feishu_delivery":"create_document"})
            assert refused["code"]=="outside_scope"
            assigned=await rt.team_jobs.delegate(run,{"agent_ids":[analyst.id],"brief":"Read the local materials"})
            assert assigned["receipts"][0]["status"]=="assigned"
            return TurnOutcome("succeeded",result="已派给员工。")
        await sink.emit("assistant.message",{"text":INVENTED})
        return TurnOutcome("succeeded",result=INVENTED)
    engine.handlers[chat.id]=steward
    await rt.send_turn(chat.id,"Create the cloud deliverable and analyze the local material.")
    async def terminal():
        root=await root_of(sessions,chat.id)
        return root if root and root.status in ("completed","failed") else None
    root=await wait_until(terminal)
    last=(await turns_of(sessions,chat.id))[-1]
    assert root.outcome=="missing_delivery" and root.outcome_detail==last.result
    assert "还没登记" in last.result and analyst.name in last.result and INVENTED not in last.result
