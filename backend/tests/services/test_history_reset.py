import json
from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from app.services import employee_runtime, history_reset as history, local_claude_runner, memory_journal


@pytest.fixture
def profile(tmp_path, monkeypatch):
    root = tmp_path / "data"
    channel = root / "gateway" / "channel.sqlite3"
    channel.parent.mkdir(parents=True)
    cfg = {"owner_id":"owner", "steward_thread":1, "steward":{"agent_id":1, "app_id":"a"}}
    monkeypatch.setattr(history, "_configuration", lambda: (root,channel,cfg))
    monkeypatch.setattr(employee_runtime, "_default_service_root", lambda: root/"employee-runtime")
    monkeypatch.delenv("SUPARAGENT_EMPLOYEE_RUNTIME_ROOT",raising=False)
    monkeypatch.setattr(local_claude_runner, "workspace_dir", lambda: root/"workspace/workspace")
    monkeypatch.setattr(memory_journal, "state_root", lambda: root/"memory-state")
    with closing(sqlite3.connect(root/"suparagent.db")) as db:
        db.executescript("""
        create table thread(id integer primary key, owner_id, agent_id, kind, archived_at, room_id,
            last_seq, updated_at, provider_session_id, session_state, provider_state);
        insert into thread values(1,'owner',1,'chat',null,null,12,'today','native','active','{"effort":"medium"}'),
            (2,'owner',1,'job',null,null,2,'today','job','active',null),
            (3,'owner',1,'room',null,4,0,'today',null,'new',null);
        create table agent(id integer primary key, owner_id, name); insert into agent values(1,'owner','Boss');
        create table team(owner_id); create table work_item(owner_id); insert into work_item values('owner');
        create table turn(thread_id,status); insert into turn values(1,'done');
        create table approval(status); insert into approval values('allowed');
        create table agent_job(status); insert into agent_job values('completed');
        create table event(body); insert into event values('message'); create table agent_job_event(body);
        create table needs_you_state(card_hash,card_thread_id,card_day,notified);
        create table permission_rule(user_id,bucket,connector);
        insert into permission_rule values ('owner','employee_project_command','{"employee_id":1}'),
             ('owner','employee_project_command','{"employee_id":1,"task_id":3}');
        create table schedule(name); insert into schedule values('weekly');
        """)
    with closing(sqlite3.connect(channel)) as db:
        db.executescript("""
        create table inbox(payload,state,task,error); insert into inbox values('message','done',1,null);
        create table outbox(state); insert into outbox values('sent');
        create table identities(name); insert into identities values('bound');
        create table knowledge_documents(document); insert into knowledge_documents values('published');
        """)
    return root,channel


def test_preview_reset_preserves_profiles_and_checks_result(profile):
    root, channel = profile
    role = root/"workspace/workspace/agents/1"
    role.mkdir(parents=True)
    (role/"AGENTS.md").write_text('role')
    (role/"MEMORY.md").write_text('former conversations')
    engine = root/"deepseek-codex"
    (engine/"sessions").mkdir(parents=True)
    (engine/"sessions/chat.jsonl").write_text('chat')
    (engine/"auth.json").write_text('credentials')
    with closing(sqlite3.connect(engine/"logs_2.sqlite")) as db:
        db.executescript("create table logs(entry); insert into logs values('old log')")
    preview = history.preview()
    assert (preview["conversations"],preview["turns"],preview["tasks"],preview["jobs"]) == (2,1,1,1)
    result = history.clear(preview["revision"])
    assert all(result[k] == 0 for k in ("conversations","tasks","turns","jobs","approvals"))
    assert not (engine/"sessions/chat.jsonl").exists() and not (role/"MEMORY.md").exists()
    assert (engine/"auth.json").read_text() == 'credentials' and (role/"AGENTS.md").read_text() == 'role'
    with closing(sqlite3.connect(root/"suparagent.db")) as db:
        assert db.execute("select id from thread order by id").fetchall() == [(1,),(3,)]
        assert db.execute("select provider_session_id,provider_state from thread where id=1").fetchone() == (None,'{"effort":"medium"}')
        assert db.execute("select * from schedule").fetchall() == [('weekly',)]
        assert db.execute("select connector from permission_rule").fetchall() == [('{"employee_id":1}',)]
    with closing(sqlite3.connect(channel)) as db:
        assert db.execute("select * from inbox").fetchall() == [('{}','ignored',None,None)]
        assert db.execute("select * from knowledge_documents").fetchall() == [('published',)]
        assert db.execute("select * from identities").fetchall() == [('bound',)]


def test_stale_or_active_job_rejected_before_any_deletion(profile):
    root,_ = profile
    first = history.preview()["revision"]
    with closing(sqlite3.connect(root/"suparagent.db")) as db:
        db.execute("insert into turn values(1,'done')")
        db.commit()
    with pytest.raises(ValueError):
        history.clear(first)
    with closing(sqlite3.connect(root/"suparagent.db")) as db:
        db.execute("insert into agent_job values('running')")
        db.commit()
    now = history.preview()
    assert now["busy"] and now["turns"] == 2
    with pytest.raises(ValueError):
        history.clear(now["revision"])
    assert history.preview() == now


def test_live_gateway_lease_blocks_destructive_action(profile):
    _,channel = profile
    path = channel.parent/"gateway.lock"
    with path.open("a+b") as stream:
        assert history.data_lease._try_lock(stream.fileno())
        with pytest.raises(RuntimeError):
            history.clear(history.preview()["revision"])
    assert history.preview()["jobs"] == 1

def test_migrated_workbench_and_real_gateway_schema(tmp_path, monkeypatch):
    from app.feishu.routing import journal
    from tests.db.sqlite_helpers import run_alembic

    root = tmp_path / "workbench"
    root.mkdir()
    database = root/"suparagent.db"
    migration = run_alembic(database, "upgrade", "head")
    assert migration.returncode == 0, migration.stderr
    channel = root/"channel.sqlite3"
    journal(channel).close()
    with closing(sqlite3.connect(database)) as db:
        db.execute("insert into agent (id,owner_id,name,provider) values(1,'owner','steward','codex')")
        db.execute("insert into thread (id,owner_id,agent_id,provider) values(1,'owner',1,'codex')")
        db.execute("insert into thread (id,owner_id,agent_id,provider) values(2,'owner',1,'codex')")
        db.commit()
    cfg = {"owner_id":"owner", "steward_thread":1, "steward":{"agent_id":1,"app_id":"gateway"}}
    monkeypatch.setattr(history,"_configuration",lambda:(root,channel,cfg))
    monkeypatch.setattr(local_claude_runner,"workspace_dir",lambda:root/"workspace/workspace")
    monkeypatch.setattr(memory_journal,"state_root",lambda:root/"memory-state")
    monkeypatch.setattr(employee_runtime,"_default_service_root",lambda:root/"employee-runtime")
    monkeypatch.delenv("SUPARAGENT_EMPLOYEE_RUNTIME_ROOT",raising=False)
    before = history.preview()
    assert before["conversations"] == 1
    assert history.clear(before["revision"])["conversations"] == 0
