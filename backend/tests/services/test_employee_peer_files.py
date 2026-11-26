import sqlite3

import pytest

from app.services.feature_files import FeatureFiles


def test_granted_employee_can_write_own_and_only_read_active_peer(tmp_path):
    paths=[tmp_path/n for n in ("steward","dog","peer","old","other")]
    for p in paths: p.mkdir()
    task=paths[1]/"任务"/"3"
    task.mkdir(parents=True)
    (paths[2]/"note.txt").write_text("hello")
    dbpath=tmp_path/"host"/"db"
    dbpath.parent.mkdir()
    with sqlite3.connect(dbpath) as db:
        db.executescript("create table team(id,owner_id,lead_agent_id);create table agent(id,owner_id,team_id,name,role_path,archived_at,team_workspaces_readable);insert into team values(1,'me',1);")
        for i,p in enumerate(paths,1):
            db.execute("insert into agent values(?,?,?,?,?,?,?)",(i,"other" if i==5 else "me",1,p.name,str(p),"inactive" if i==4 else None,i==2))
    fs=FeatureFiles(task,True,{"database":str(dbpath),"employee":2,"owner":"me"})
    assert list(fs.peer_roots())==["employee:3:peer"]
    fs.call("write_text",dict(scope="role",path="notes/a.txt",text="saved"))
    assert (paths[1]/"notes"/"a.txt").read_text()=="saved"
    assert fs.call("read_text",dict(scope="employee:3:peer",path="note.txt"))["text"]=="hello"
    for scope,path in [("employee:3:peer","note.txt"),("role","AGENTS.md"),("role","../peer/note.txt")]:
        with pytest.raises(ValueError):
            fs.call("write_text",dict(scope=scope,path=path,text="invalid"))
    with sqlite3.connect(dbpath) as db:
        nested=paths[2]/"private";nested.mkdir()
        db.execute("update agent set role_path=? where id=5",(str(nested),))
    assert not fs.peer_roots()
    with sqlite3.connect(dbpath) as db:
        db.execute("update agent set team_workspaces_readable=0 where id=2")
    with pytest.raises(ValueError):
        fs.call("list_files",dict(scope="employee:3:peer",path=""))
    fs.call("write_text",dict(scope="role",path="own.txt",text="我的材料"))
    assert (paths[1]/"own.txt").read_text("utf8")=="我的材料"
    with sqlite3.connect(dbpath) as db:
        nested=paths[1]/"other";nested.mkdir()
        db.execute("update agent set role_path=? where id=5",(str(nested),))
    with pytest.raises(ValueError):
        fs.call("write_text",dict(scope="role",path="other/unsafe.txt",text="bad"))


def test_unbound_task_cannot_select_peer_or_write_role(tmp_path):
    task=tmp_path/"任务"/"1";task.mkdir(parents=True)
    fs=FeatureFiles(task,True)
    with pytest.raises(ValueError,match="任务目录"): fs.call("write_text",dict(scope="role",path="n",text="a"))
    with pytest.raises(ValueError): fs.call("list_files",dict(scope="employee:3:peer",path=""))
    with pytest.raises(ValueError,match="本轮.*只读"):
        FeatureFiles(task,False).call("write_text",dict(scope="role",path="n",text="a"))


@pytest.mark.asyncio
async def test_managed_edit_mode_confines_automatic_approval(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import Mock, AsyncMock
    from app.services.codex_adapter import _CodexTurn

    role = tmp_path / "employee"
    task = role / "任务" / "2"
    task.mkdir(parents=True)
    database = tmp_path / "host" / "db.sqlite3"
    database.parent.mkdir()
    with sqlite3.connect(database) as db:
        db.executescript("create table team(id,owner_id,lead_agent_id);create table agent(id,owner_id,team_id,name,role_path,archived_at,team_workspaces_readable);insert into team values(1,'me',1);")
        db.execute("insert into agent values(2,'me',1,'worker',?,null,0)", (str(role),))
    run = Mock()
    run.ctx = SimpleNamespace(consult_only=False, peer_workspaces={"owner":"me","employee":2,"database":str(database)})
    run.workspace, run.feature, run.computer, run.tools = task, task, None, True
    run.gate.open = AsyncMock(return_value=None)
    run.requests, run.permit_inputs = {}, {}
    request={"method":"mcpServer/elicitation/request","id":9,
             "params":{"serverName":"feature_files","_meta":{"codex_approval_kind":"mcp_tool_call"}}}
    for mode,scope,path,decision in [
        ("accept_edits","task","a.md","accept"), ("accept_edits","role","a.md","accept"),
        ("accept_edits","employee:3:peer","a.md","decline"), ("accept_edits","role","AGENTS.md","decline"),
        ("accept_edits","task","../1/a","decline"), ("plan","task","a.md","decline")]:
        run.mode = mode
        run._computer_call.return_value = ("write_text",{"scope":scope,"path":path,"text":"ok"})
        await _CodexTurn._server_request(run,request)
        assert run.rpc.respond.call_args.args[1]["action"] == decision
    run.mode = "ask"
    await _CodexTurn._server_request(run,request)
    run.gate.open.assert_awaited_once()
