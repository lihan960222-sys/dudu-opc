from hashlib import sha256
import os
from types import SimpleNamespace

import pytest

from app.services import employee_project_files as files
from app.services import employee_terminal
from app.team import mcp_server, policy


def test_one_existing_text_edit_preserves_line_endings_and_checks_revision(tmp_path):
    root = tmp_path / "role"
    task = root / "任务" / "7"
    task.mkdir(parents=True)
    private = tmp_path / "private"
    private.mkdir()
    project = SimpleNamespace(root=root, task_dir=task, writable=True, profile=private)
    source = root / "seconds.cmd"
    original = b"@echo off\r\nset /a value=999/100\r\n"
    source.write_bytes(original)
    before = files.read(project, {"path": "seconds.cmd"})
    assert before["text"].encode() == original
    change = {"path":"seconds.cmd", "sha256":before["sha256"], "old":"999/100", "new":"999/1000"}
    result = files.edit(project, change, storage=private)
    assert source.read_bytes() == original.replace(b"999/100", b"999/1000")
    assert result["sha256"] == sha256(source.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        files.edit(project, change, storage=private)
    assert {p.name for p in root.iterdir()} == {"任务","seconds.cmd"}


def test_peer_listing_counts_only_visible_matches_and_signals_if_more_exist(tmp_path):
    for name in ("1-alpha", "3-beta", ".private"):
        (tmp_path / name).mkdir()
    for name in ("0-before.txt", "2-between.txt", "4-after.txt"):
        (tmp_path / name).touch()
    for limit, folders_only, names, truncated in (
        (2, False, ["0-before.txt", "1-alpha"], True),
        (1, True, ["1-alpha"], True),
        (2, True, ["1-alpha", "3-beta"], False),
        (3, True, ["1-alpha", "3-beta"], False),
    ):
        result = files.inspect_workspace(tmp_path, "", listing=True, limit=limit, folders_only=folders_only)
        assert [entry["name"] for entry in result["entries"]] == names
        assert result["truncated"] is truncated
    for args in ({"limit": 0}, {"limit": 501}, {"limit": True}, {"limit": "2"}, {"folders_only": 1}):
        with pytest.raises(ValueError):
            files.inspect_workspace(tmp_path, "", listing=True, **args)


@pytest.mark.parametrize("name", ["../peer.txt", ".env", "AGENTS.md", "任务/7/notes.txt", "C:/wild.txt", "source.txt:stream"])
def test_files_stay_in_authorized_employee_project(tmp_path, name):
    task = tmp_path / "任务" / "7"
    task.mkdir(parents=True)
    project = SimpleNamespace(root=tmp_path, task_dir=task, writable=False)
    with pytest.raises((ValueError, OSError)):
        files.read(project, {"path":name})
    with pytest.raises(ValueError):
        files.edit(project, {"path":"ok.txt","sha256":"1"*64,"old":"x","new":"y"}, storage=tmp_path)


def test_read_only_assignment_can_inspect_without_shell_or_edits(tmp_path):
    team=policy.TurnTeam(1,2,False,False,strict_hierarchy=True,feature_consult=True)
    ctx=SimpleNamespace(project_runtime=SimpleNamespace(writable=False),approval_mode="plan",team=team)
    assert employee_terminal.allowed_tools(ctx)==("list_project_files","read_project_text")
    server=mcp_server.Server(tmp_path,project_consult=True,project_read=True)
    listed={t["name"] for t in server.tools}
    assert "read_project_text" in listed and "run_project_command" not in listed and "replace_project_text" not in listed
    assert policy.automatic("mcp__team__read_project_text","plan",team,{"path":"source.txt"})[0]=="allowed"
    for name in ("replace_project_text", "run_project_command"):
        assert policy.automatic("mcp__team__"+name,"plan",team,{})[0]=="denied"
    ctx.scheduled_knowledge=True
    assert not employee_terminal.allowed_tools(ctx)


def test_standing_edits_are_separate_from_one_operation_command_decisions():
    team=policy.TurnTeam(1,2,False,False,strict_hierarchy=True,feature_consult=True)
    ctx=SimpleNamespace(project_runtime=SimpleNamespace(writable=True),approval_mode="accept_edits",team=team)
    assert employee_terminal.allowed_tools(ctx)==("run_project_command","list_project_files","read_project_text")
    for mode in ("ask", "plan", "accept_edits"):
        for tool in ("list_project_files", "read_project_text"):
            assert policy.automatic("mcp__team__"+tool,mode,team,{}) == ("allowed","read_only_employee_project")
    assert policy.automatic("mcp__team__replace_project_text","accept_edits",team,{})[0]=="denied"
    assert policy.automatic("mcp__team__replace_project_text","ask",team,{}) is None
    assert policy.automatic("mcp__team__run_project_command","accept_edits",team,{"argv":["anything"]}) is None


def test_ambiguous_matching_text_and_a_hard_link_are_rejected(tmp_path):
    role = tmp_path / "role"
    task = role / "任务" / "2"
    task.mkdir(parents=True)
    project=SimpleNamespace(root=role,task_dir=task,writable=True)
    source=role / "source.txt"
    source.write_bytes(b"old old\n")
    args={"path":"source.txt","old":"old","new":"new","sha256":sha256(b"old old\n").hexdigest()}
    with pytest.raises(ValueError):
        files.edit(project,args,storage=tmp_path)
    assert source.read_bytes()==b"old old\n"
    os.link(source,tmp_path / "another-owner.txt")
    with pytest.raises(ValueError):
        files.read(project,{"path":"source.txt"})
    with pytest.raises(ValueError):
        files.edit(project,args,storage=tmp_path)
