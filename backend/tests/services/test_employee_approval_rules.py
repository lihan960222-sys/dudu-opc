import json
import shlex
from types import SimpleNamespace

import pytest

from app.services import employee_approval_rules as rules
from tests.services.thread_harness import cleanup_jobs, owner, sessions


def request(tmp_path, monkeypatch, script):
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    exe = tmp_path / "System32/WindowsPowerShell/v1.0/powershell.exe"
    argv = [str(exe), "-NoProfile", "-Command", script]
    project = SimpleNamespace(root=tmp_path, employee_id=2, permission_name="employee-scope")
    return project, {"argv": argv, "command": shlex.join(argv), "cwd": str(tmp_path)}


def test_exact_grant_does_not_cross_employees_profiles_or_permission_changes(tmp_path, monkeypatch):
    project, args = request(tmp_path, monkeypatch, "Get-Location")
    key = rules.identity(project,"Bash",args)
    assert key is not None
    assert rules.identity(project,"Bash",{**args,"command":"other"}) is None
    assert rules.identity(project,"Bash",{**args,"additionalPermissions":{"network":{"enabled":True}}}) is None
    project.employee_id = 3
    assert rules.identity(project,"Bash",args) != key
    project.employee_id = 2
    project.permission_name = "different"
    assert rules.identity(project,"Bash",args) != key


def test_application_grant_pins_installed_file_contents(tmp_path, monkeypatch):
    installed = tmp_path/"Program Files"
    installed.mkdir()
    monkeypatch.setenv("ProgramFiles",str(installed))
    batch = installed/"example.bat"
    batch.write_text("version 1")
    project,args = request(tmp_path,monkeypatch,f"Start-Process -FilePath '{batch}' -PassThru")
    key = rules.identity(project,"Bash",args)
    assert key and json.loads(key[1])["target_sha256"]
    batch.write_text("version 2")
    assert rules.identity(project,"Bash",args) != key
    for script in (f"Start-Process -FilePath '{tmp_path/'copy.lnk'}'", "Get-Location; other", ".\\verify.cmd"):
        project,args = request(tmp_path,monkeypatch,script)
        assert rules.identity(project, "Bash", args) is None


def test_installed_codex_pwsh_no_profile_wrapper(tmp_path, monkeypatch):
    installed = tmp_path / "Program Files"
    installed.mkdir()
    monkeypatch.setenv("ProgramFiles", str(installed))
    pwsh = installed / "pwsh.exe"
    pwsh.touch()
    (installed / "System.Management.Automation.dll").touch()
    project, _ = request(tmp_path, monkeypatch, "Get-Location")
    cmd = f'"{pwsh}" -NoProfile -Command "Get-Location"'
    def admissible(command):
        return rules.readonly_request(project, "Bash", {"command": command, "cwd": str(tmp_path)})
    assert admissible(cmd)
    assert not admissible(cmd.replace(" -NoProfile", ""))
    assert not admissible(cmd.replace(str(pwsh), str(tmp_path / "pwsh.exe")))


def test_owner_can_remember_one_named_process_query_without_granting_a_shell(tmp_path, monkeypatch):
    script = "Get-Process -Name kit,python -ErrorAction SilentlyContinue | Select-Object Id,ProcessName,Path"
    project, args = request(tmp_path, monkeypatch, script)
    assert rules.identity(project, "Bash", args)
    assert not rules.readonly_request(project, "Bash", args)  # Outside the default project-read grant.
    for other in (script.replace("kit,python", "*"), script + "; Stop-Process -Name kit",
                  script.replace("Id,ProcessName,Path", "*")):
        project, args = request(tmp_path, monkeypatch, other)
        assert not rules.identity(project, "Bash", args)


@pytest.mark.asyncio
async def test_standing_grant_roundtrip_and_revocation(tmp_path,monkeypatch,db_session):
    from sqlalchemy import delete
    from app.db.models.permission_rule import PermissionRule
    project,args = request(tmp_path,monkeypatch,"Get-ChildItem -Name")
    key = rules.identity(project,"Bash",args)
    assert key and not await rules.find(db_session,"owner",key)
    await rules.stage(db_session,"owner",key)
    row = await rules.find(db_session,"owner",key)
    assert rules.decode(row)["command"] == args["command"]
    assert not await rules.find(db_session,"another_owner",key)
    await db_session.execute(delete(PermissionRule).where(PermissionRule.id == row.id))
    assert not await rules.find(db_session,"owner",key)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ask", "plan"])
async def test_original_request_delivers_then_fresh_task_reuses_owner_grant(sessions,owner,cleanup_jobs,tmp_path,monkeypatch,mode):
    from app.db.models import AgentJob
    from app.services import agent_store,employee_runtime,feature_tasks,role_workspace,thread_store
    from app.services.thread_runtime import ActiveRun,ThreadApprovalGate,ThreadRuntime
    from app.team import service
    root = tmp_path / "work"
    root.mkdir()
    async with sessions() as db:
        lead = await agent_store.create_agent(db, owner_id=owner, name="Boss", provider="codex", make_lead=True)
        worker = await agent_store.create_agent(db, owner_id=owner, name="Employee", provider="codex", approval_mode=mode)
        role_workspace.initialize(root, "gpt-fake", "Inspect your project.")
        worker.role_path = str(root)
        await service.change_member(db,owner,worker.id)
        await db.commit()
    employee_runtime.save_terminal_settings(owner, worker.id, employee_runtime.TerminalSettings(True, outbound_network=True))
    for attempt in (1,2,3):
        title = f"Task {attempt}"
        async with sessions() as db:
            task = (await feature_tasks.route(db,owner_id=owner,agent_id=worker.id,project="P",event_key=title,
                                              title=title,text=title))["task_id"]
            thread,_ = await feature_tasks.session_for(db,owner,task,model="gpt-fake",workspace_path=str(root))
            project = await feature_tasks.project_runtime_for(db,owner,worker,thread,mode)
            turn = await thread_store.start_turn(db,thread.id,title)
            db.add(AgentJob(owner_id=owner,task_id=task,project="P",assignment_epoch=1,requester_agent_id=lead.id,
                requester_thread_id=thread.id,requester_turn_id=turn.id,assignee_agent_id=worker.id,
                assignee_thread_id=thread.id,assignee_turn_id=turn.id,current_turn_id=turn.id,kind="job",mode="work",
                status="running",brief=title,brief_hash=title,approval_mode=mode))
            await db.commit()
        _,args = request(root,monkeypatch,"Get-Location")
        ctx = SimpleNamespace(feature_workspace=str(root),workspace=root,approval_mode=mode,aborted=False,
                              project_runtime=project)
        run = ActiveRun(thread.id,turn.id,turn.owner_token,ctx,approval_timeout=3)
        rt = SimpleNamespace(session=sessions,owner_id=owner,_runs={turn.id:run},_waiters={},_mode_of=lambda _: "ask",
                             hub=SimpleNamespace(notify=lambda _:None))
        gate = run.gate = ThreadApprovalGate(rt,run)
        handle = await gate.open(request_id=title,tool="Bash",kind="command",input=args,summary="Get-Location")
        if attempt == 1:
            record = {}
            from sqlalchemy import select
            from app.services.employee_approval_history import pending_for_jobs
            async with sessions() as db:
                job = await db.scalar(select(AgentJob).where(AgentJob.assignee_turn_id == turn.id))
                pending = await pending_for_jobs(db, owner, [job])
                assert pending[job.id] == [{"request_id":handle.approval_id, "tool":"Bash", "action":"Get-Location"}]
            assert await ThreadRuntime.answer_approval(rt,handle.approval_id,"allow",remember=True,saved_project_rule=record,
                expected_turn_id=turn.id,expected_assignment_epoch=1,expected_employee_id=worker.id)
            assert record["id"]
            async with sessions() as db:
                assert not await pending_for_jobs(db, owner, [job])
        if attempt == 3:
            # A rule matched when the card opened, but the live profile lost its
            # network grant before the Allow could be delivered to Codex.
            employee_runtime.save_terminal_settings(owner, worker.id, employee_runtime.TerminalSettings(True))
            assert (await gate.decide(handle)).kind == "deny"
            async with sessions() as db:
                row = await thread_store.get_approval(db, handle.approval_id)
                assert row.status == "denied" and row.delivered_at is None
            continue
        assert (await gate.decide(handle)).kind == "allow"
        async with sessions() as db:
            row = await thread_store.get_approval(db,handle.approval_id)
            assert row.delivered_at and row.status == "allowed"
