from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import employee_check_review
from app.services.employee_runtime import ProjectRuntime


def setup_check(tmp_path):
    role=tmp_path/"role"
    tests=role/"tests"
    tests.mkdir(parents=True)
    (tests/"verify.cmd").write_bytes(b"@echo off\r\necho synthetic verification\r\n")
    job=SimpleNamespace(id=27,task_id=7,owner_id="mine",assignee_agent_id=2,assignment_epoch=3)
    project=ProjectRuntime("mine",2,7,3,"openai",role,True,(tmp_path/"peer",),
                           profile_path=tmp_path/"state"/"profiles"/"mine"/"2"/"openai")
    contract={"kind":"commands","commands":[{"command":"cmd.exe /d /c verify.cmd","cwd":"tests"}]}
    return project,job,contract


def test_only_declared_script_with_unchanged_contents_and_epoch_can_run(tmp_path):
    project,job,contract=setup_check(tmp_path)
    choices=employee_check_review.capture(job,project.root,contract)
    assert len(choices)==1
    bound=choices[0]
    args={"argv":["cmd.exe","/d","/c","verify.cmd"],"cwd":"tests"}
    with employee_check_review.pinned(project,job.id,args,bound):
        pass
    for changed in ({**args,"cwd":"."}, {**args,"argv":args["argv"]+["other"]},
                    {**args,"argv":["cmd.exe","/d","/c","verify.cmd & whoami"]}):
        with pytest.raises(ValueError):
            with employee_check_review.pinned(project,job.id,changed,bound):
                pass
    for changed in ({"epoch": 4}, {"employee": 5}, {"owner": "another"}, {"task": 8},
                    {"job": 24}, {"root": str(project.root.parent)}):
        with pytest.raises(ValueError):
            with employee_check_review.pinned(project, job.id, args, {**bound, **changed}):
                pass
    (project.root/"tests"/"verify.cmd").write_bytes(b"@echo off\r\necho changed\r\n")
    with pytest.raises(ValueError):
        with employee_check_review.pinned(project,job.id,args,bound):
            pass


def test_private_check_uses_narrower_named_read_only_permissions(tmp_path):
    project,_,_=setup_check(tmp_path)
    options=project.cli_options()
    key=f"permissions.{project.check_permission_name}."
    fs=next(text for text in options if text.startswith(key+"filesystem="))
    assert '"write"' not in fs and str(project.root).replace("\\","\\\\") in fs
    assert str(project.readable_roots[0]).replace("\\","\\\\") not in fs
    assert key+"network.enabled=false" in options
    assert f'default_permissions="{project.permission_name}"' in options


@pytest.mark.parametrize("value",["cmd.exe /d /c verify.cmd && whoami","cmd.exe /d /c ..\\verify.cmd",
    "powershell.exe -Command anything","cmd.exe /d /c repair.cmd","cmd.exe /d /c verify.cmd extra"])
def test_general_commands_never_become_steward_checks(tmp_path,value):
    project,job,contract=setup_check(tmp_path)
    contract["commands"][0]["command"]=value
    assert employee_check_review.capture(job,project.root,contract)==[]


@pytest.mark.asyncio
async def test_command_rpc_holds_the_reviewed_script_and_records_the_narrow_profile(tmp_path):
    import os
    from pathlib import Path
    from app.services import employee_terminal
    from tests.services.test_employee_terminal import project_context

    project, job, contract = setup_check(tmp_path)
    scope, = employee_check_review.capture(job, project.root, contract)
    script = project.root / "tests" / "verify.cmd"
    args = {"argv": scope["argv"], "cwd": scope["cwd"]}
    ctx = project_context(project.root)
    ctx.project_runtime, ctx.approval_mode = project, "accept_edits"

    async def command(method, params, **kwargs):
        if method == "permissionProfile/list":
            assert params == {"cwd": str(script.parent)}
            return {"data": [{"id": project.check_permission_name, "allowed": True}]}
        assert method == "command/exec" and params["permissionProfile"] == project.check_permission_name
        assert params["cwd"] == str(script.parent) and params["command"][1:] == args["argv"][1:]
        if os.name == "nt":
            assert Path(params["command"][0]) == Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe"
            with pytest.raises(PermissionError):
                script.open("r+b").close()
        return {"exitCode": 0, "stdout": "fixture passed", "stderr": ""}

    run = SimpleNamespace(ctx=ctx, project=project, rpc=SimpleNamespace(closed=False, request=AsyncMock(side_effect=command)),
        clock=SimpleNamespace(remaining=lambda: 30), completed=False, thread_id="private", codex_turn_id="turn",
        sink=SimpleNamespace(lost=False), _emit=AsyncMock(return_value=True))
    assert (await employee_terminal.execute(run, args, {}, scope=scope))["exit_code"] == 0
    completed = run._emit.await_args.args[1]
    assert completed["ok"] and not completed["file_receipts"]
    assert completed["command_receipt"]["permission_profile"] == project.check_permission_name
    assert completed["command_receipt"]["script_sha256"] == scope["sha256"]
    assert completed["command_receipt"]["decided_by"] == employee_check_review.SOURCE
    assert [call.args[0] for call in run.rpc.request.await_args_list] == ["permissionProfile/list", "command/exec"]

    run.rpc.request = AsyncMock(return_value={"data": [{"id": project.check_permission_name, "allowed": False}]})
    with pytest.raises(ValueError, match="只读验收权限"):
        await employee_terminal.execute(run, args, {}, scope=scope)
    assert [call.args[0] for call in run.rpc.request.await_args_list] == ["permissionProfile/list"]

    script.write_bytes(b"@echo off\r\necho changed\r\n")
    with pytest.raises(ValueError, match="已变更"):
        await employee_terminal.execute(run, args, {}, scope=scope)
    assert run.rpc.request.await_count == 1
    kind, failure = run._emit.await_args.args
    assert kind == "tool.completed" and not failure["ok"] and "command_receipt" not in failure


@pytest.mark.asyncio
async def test_real_dispatch_and_gate_bind_one_review_to_one_unchanged_check(sessions,owner,tmp_path,monkeypatch):
    import json
    from sqlalchemy import select
    from app.auth import people
    from app.core.config import settings
    from app.db.models import AgentJob,AgentJobEvent,Event,FeatureSession,WorkItem
    from app.services import agent_store,employee_runtime,thread_store
    from app.services.codex_adapter import CodexAdapter
    from app.services.thread_hub import ThreadHub
    from app.services.thread_runtime import ThreadApprovalGate, ThreadRuntime
    from app.team import policy,service
    from app.team.bridge import TeamBridge,consume_allowed_approval
    from app.team.jobs import Jobs

    frame,_,contract=setup_check(tmp_path)
    lead_folder=tmp_path/"lead"
    lead_folder.mkdir()
    monkeypatch.setattr(settings,"STEWARD_STRICT_OWNER",owner)
    monkeypatch.setattr(employee_runtime,"native_enabled",lambda o,e:o==owner and e==staff.id)
    async with sessions() as db:
        lead=await agent_store.create_agent(db,owner_id=owner,name="Lead",provider="codex",make_lead=True,workspace_path=str(lead_folder))
        staff=await agent_store.create_agent(db,owner_id=owner,name="Worker",provider="codex",approval_mode="accept_edits",
                                            workspace_path=str(frame.root))
        await service.change_member(db,owner,staff.id)
        staff.role_path=str(frame.root)
        active=await people.ensure_owner_person(db)
        chat=await thread_store.create_thread(db,owner_id=owner,agent_id=lead.id,provider="codex",workspace_path=str(lead_folder))
        source=await thread_store.start_turn(db,chat.id,"Verify the project",origin="person",person_id=active.id,use_employee_role=True)
    rt=SimpleNamespace(session=sessions,owner_id=owner,adapters={"codex":CodexAdapter()},hub=ThreadHub(),_waiters={})
    dispatch=SimpleNamespace(thread_id=chat.id,turn_id=source.id,agent_id=lead.id,owner_token=source.owner_token,
        ctx=SimpleNamespace(approval_mode="ask",team=SimpleNamespace(team_id=lead.team_id,origin="person")))
    job=await Jobs(rt).enqueue(dispatch,{"agent_id":staff.id,"project":"Synthetic","brief":"Verify","done_when":"Passing check",
                                        "completion_check":contract},reply="resume")
    async with sessions() as db:
        bound=await db.scalar(select(AgentJobEvent.detail).where(AgentJobEvent.job_id==job.id,AgentJobEvent.kind==employee_check_review.EVENT))
        assert bound and len(bound["checks"])==1
        feature=await thread_store.create_thread(db,owner_id=owner,agent_id=staff.id,provider="codex",kind="feature",
                                                 workspace_path=str(frame.root))
        db.add(FeatureSession(thread_id=feature.id,task_id=job.task_id,assignment_epoch=1))
        item=await db.get(WorkItem,job.task_id)
        item.thread_id=feature.id
        worker=await thread_store.start_turn(db,feature.id,"Check",internal=True)
        current=await db.get(AgentJob,job.id)
        current.status="running"
        current.assignee_thread_id=feature.id
        current.assignee_turn_id=current.current_turn_id=worker.id
        await db.commit()
    project=ProjectRuntime(owner,staff.id,job.task_id,1,"openai",frame.root,True,(),profile_path=frame.profile)
    ctx=SimpleNamespace(approval_mode="accept_edits",project_runtime=project,workspace=frame.root,aborted=False,
        team=policy.TurnTeam(lead.team_id,staff.id,False,False,feature_consult=True,strict_hierarchy=True))
    run=SimpleNamespace(thread_id=feature.id,turn_id=worker.id,agent_id=staff.id,owner_token=worker.owner_token,
                        ctx=ctx,recovery_call=None,approval_timeout=60)
    gate=ThreadApprovalGate(rt,run)
    args={"argv":["cmd.exe","/d","/c","verify.cmd"],"cwd":"tests"}
    name="mcp__team__run_project_command"
    card=await gate.open(request_id="exact",tool=name,input=args,kind="command",summary="check")
    await gate.decide(card)
    async with sessions() as db:
        trace=await db.scalar(select(Event.data).where(Event.turn_id==worker.id,Event.type=="approval.resolved"))
        assert trace["by"]==employee_check_review.SOURCE and trace["project_check"]==bound["checks"][0]
        assert await consume_allowed_approval(db,worker.id,worker.owner_token,name,args,return_id=True)==card.approval_id
        assert not await consume_allowed_approval(db,worker.id,worker.owner_token,name,args)
    bridge=TeamBridge(rt,run)
    rt.adapters["codex"]=SimpleNamespace(run_project_command=AsyncMock(return_value={"exit_code":0}))
    try:
        await bridge.execute("run_project_command",args,approval_id=card.approval_id)
        assert rt.adapters["codex"].run_project_command.await_args.kwargs["scope"]==bound["checks"][0]
        (frame.root/"tests"/"verify.cmd").write_bytes(b"@echo off\r\necho replacement\r\n")
        with pytest.raises(service.TeamError,match="已变更"):
            await bridge.execute("run_project_command",args,approval_id=card.approval_id)
        assert rt.adapters["codex"].run_project_command.await_count==1
        new=await gate.open(request_id="after-change",tool=name,input=args,kind="command",summary="changed")
        from app.db.models import Approval
        async with sessions() as db:
            assert (await db.get(Approval,new.approval_id)).status=="pending"
        run.gate = gate
        run.stop_requested = False
        rt._runs = {worker.id: run}
        rt._mode_of = lambda _: ctx.approval_mode
        assert await ThreadRuntime.answer_approval(rt, new.approval_id, "allow", person_id=active.id)
        await gate.decide(new)
        async def submit(label):
            request = bridge.folder / f"req-{label}.working"
            request.write_text(json.dumps({"name": "run_project_command", "arguments": args}), encoding="utf-8")
            await bridge.handle(request)
            return json.loads((bridge.folder / f"resp-{label}.json").read_text(encoding="utf-8"))

        assert (await submit("person"))["exit_code"] == 0
        assert rt.adapters["codex"].run_project_command.await_args.kwargs["scope"] is None
        assert (await submit("reused"))["code"] == "not_approved"
        revoked = await gate.open(request_id="revoked", tool=name, input=args, kind="command", summary="changed")
        assert await ThreadRuntime.answer_approval(rt, revoked.approval_id, "allow", person_id=active.id)
        await gate.decide(revoked)
        monkeypatch.setattr(employee_runtime, "native_enabled", lambda *_: False)
        assert (await submit("revoked"))["code"] == "outside_scope"
        assert rt.adapters["codex"].run_project_command.await_count == 2
    finally:
        await bridge.close()


from tests.services.thread_harness import owner, sessions  # noqa: E402,F401
