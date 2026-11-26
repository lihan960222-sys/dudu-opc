import hashlib
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from app.db.models import Agent, Thread
from app.team import service, work_verification as verify


def test_windows_shell_receipt_matches_one_whole_command():
    command = "cmd.exe /d /c verify.cmd"
    prefix = r'"C:\Program Files\Codex\pwsh.exe" -NoProfile -Command '
    for quoted in (f"'{command}'", f'"{command}"'):
        assert verify._matches_command(prefix + quoted, command)
        assert not verify._matches_command(prefix + quoted + "; echo done", command)
    assert not verify._matches_command(prefix + f"'{command}; echo done'", command)


def test_receipts_are_typed_and_paths_cannot_escape():
    call=("feature_files","read_text",{"scope":"role","path":"docs/note.md"})
    result={"content":[{"text":json.dumps({"text":"hello"})}]}
    assert verify.local_receipt(call,result)["sha256"]==hashlib.sha256(b"hello").hexdigest()
    assert verify.local_receipt(call,{**result,"isError":True}) is None
    assert verify.local_receipt(("fake",*call[1:]),result) is None
    for path in ("../other/a","C:/other","skills/a"):
        with pytest.raises(service.TeamError):
            verify.contract({"kind":"files","files":[{"scope":"task","path":path}]})


@pytest.mark.asyncio
async def test_native_tests_need_the_exact_command_latest_status_and_real_cwd(tmp_path):
    root = tmp_path / "employee"
    root.mkdir()
    spec = verify.contract({"kind": "commands", "commands": [{"command": "python -m pytest -q","cwd": "."}]})
    item = {"id": "call", "type": "commandExecution", "command": "python -m pytest -q",
            "cwd": str(root), "exitCode": 0, "status": "completed"}
    passed = verify.command_receipt(item, root)
    failed = verify.command_receipt({**item, "exitCode": 1}, root)
    assert passed and failed and verify.command_receipt({**item, "cwd": str(tmp_path)}, root) is None
    db = AsyncMock()
    thread = NS(workspace_path=str(root),agent_id=8)
    agent = NS(id=8,owner_id="owner",role_path=str(root))
    async def get(model, key):
        return thread if model == Thread else agent
    db.get.side_effect = get
    turn, job = NS(id=9,result="已验证"), NS(assignee_thread_id=1,assignee_agent_id=8,owner_id="owner",task_id=2)
    for records, state in [
        ([], "missing"),
        ([{"command_receipt": verify.command_receipt({**item, "command":"echo passed"},root)}],"missing"),
        ([{"command_receipt": failed},{"command_receipt": passed}],"blocked"),
        ([{"command_receipt": passed},{"command_receipt": failed}],"commands_verified"),
    ]:
        db.scalars.return_value = records
        assert (await verify.assess(db,job,turn,spec))["state"]==state


def test_native_file_change_reads_the_real_project_artifact(tmp_path):
    f = tmp_path / "binary.dat"
    f.write_bytes(bytes([0,255,0]))
    item = {"type":"fileChange","status":"completed","id":"patch",
            "changes":[{"path":str(f),"kind":{"type":"add"}}]}
    receipts = verify.file_change_receipts(item,tmp_path)
    assert receipts[0]["sha256"] == hashlib.sha256(f.read_bytes()).hexdigest()
    assert not verify.file_change_receipts({**item,"status":"failed"},tmp_path)
    assert not verify.file_change_receipts({**item,"changes":[{"path":"../elsewhere"}]},tmp_path)


def test_native_inspection_requires_a_successful_typed_action_inside_the_project(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    (root / "readme.md").write_text("hello", encoding="utf8")
    action = {"type":"read","command":"cat readme.md","path":"readme.md","name":"readme.md"}
    item = {"type":"commandExecution","id":"cmd1","cwd":str(root),"command":action["command"],
            "commandActions":[action],"status":"completed","exitCode":0}
    receipt = verify.command_receipt(item, root)
    assert receipt["inspection"] == {"operation":"read","path":"readme.md","scope":"role","root":str(root)}
    for changes in [
        {"exitCode":1}, {"status":"failed"}, {"command":"echo readme.md"}, {"commandActions":[]},
        {"commandActions":[{**action,"path":str(tmp_path)}]}, {"commandActions":[{**action,"path":"missing.md"}]},
        {"commandActions":[{**action,"type":"unknown"}]},
    ]:
        assert "inspection" not in verify.command_receipt({**item, **changes}, root)
    listing = {"type":"listFiles","command":"ls","path":None}
    assert verify.command_receipt({**item,"command":"ls","commandActions":[listing]},root)["inspection"]["path"]=="."


@pytest.mark.asyncio
async def test_file_claim_requires_actual_matching_artifact(tmp_path):
    role=tmp_path/"worker"
    folder=role/"任务"/"2"
    folder.mkdir(parents=True)
    target=folder/"deliver.md"
    target.write_text("real",encoding="utf8")
    job=NS(assignee_thread_id=1,assignee_agent_id=8,owner_id="owner",task_id=2)
    turn=NS(id=9,result="Wrote deliver.md")
    check=verify.contract({"kind":"files","files":[{"scope":"task","path":"deliver.md"}]})
    db=AsyncMock()
    async def get(model,key):
        if model==Thread: return NS(workspace_path=str(folder),agent_id=8)
        if model==Agent: return NS(role_path=str(role),owner_id="owner",id=8)
    db.get.side_effect=get
    db.scalars.return_value=[]
    assert (await verify.assess(db,job,turn,check))["state"]=="missing"
    receipt=verify.local_receipt(("feature_files","write_text",{"text":"real","path":"deliver.md"}),
                                 {"content":[{"text":'{"bytes":4}'}]})
    db.scalars.return_value=[{"file_receipt":receipt}]
    assert (await verify.assess(db,job,turn,check))["state"]=="files_verified"
    target.write_text("modified externally",encoding="utf8")
    assert (await verify.assess(db,job,turn,check))["state"]=="missing"
    turn.result="BLOCKED: Missing license"
    assert (await verify.assess(db,job,turn,check))=={"state":"blocked","detail":"Missing license"}
