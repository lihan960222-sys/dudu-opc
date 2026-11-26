import pytest

from app.auth import people
from app.core.config import settings
from app.services import agent_store, employee_runtime, thread_store
from app.team import service, steward_review
from tests.services.thread_harness import owner, sessions  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("standing,explicit,person,enabled,expected", [
    ("accept_edits",None,True,True,"accept_edits"),
    ("accept_edits","ask",True,True,"ask"),
    ("accept_edits","plan",True,True,"plan"),
    ("plan",None,True,True,"plan"),
    ("full_access",None,True,True,"accept_edits"),
    ("accept_edits",None,False,True,"ask"),
    ("accept_edits",None,True,False,"ask"),
])
async def test_owner_project_uses_employee_grant_without_inheriting_lead_default(
        sessions, owner, tmp_path, monkeypatch, standing, explicit, person, enabled, expected):
    monkeypatch.setattr(settings,"STEWARD_STRICT_OWNER",owner)
    monkeypatch.setattr(employee_runtime,"native_enabled",lambda o,e: enabled and o==owner and e==staff.id)
    async with sessions() as db:
        lead = await agent_store.create_agent(db, owner_id=owner,name="Lead",provider="codex",make_lead=True)
        staff = await agent_store.create_agent(db, owner_id=owner,name="Worker",provider="codex")
        await service.change_member(db,owner,staff.id)
        staff.role_path=str(tmp_path)
        staff.approval_mode=standing
        thread=await thread_store.create_thread(db,owner_id=owner,agent_id=lead.id,provider="codex",workspace_path=str(tmp_path))
        boss=await people.ensure_owner_person(db)
        turn=await thread_store.start_turn(db,thread.id,"Please inspect the fixture",origin="person",
            person_id=boss.id if person else None, use_employee_role=explicit is None)
        mode=await steward_review.direct_project_mode(db, owner, staff, turn.id, explicit or "ask")
        assert mode == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("read_only,expected",[(False,"accept_edits"),(True,"plan")])
async def test_scoped_job_persists_employee_mode_and_task_read_only(sessions, owner, tmp_path, monkeypatch,read_only,expected):
    from types import SimpleNamespace
    from app.services.codex_adapter import CodexAdapter
    from app.services.thread_hub import ThreadHub
    from app.team.jobs import Jobs

    monkeypatch.setattr(settings,"STEWARD_STRICT_OWNER",owner)
    monkeypatch.setattr(employee_runtime,"native_enabled",lambda o,e: o==owner and e==staff.id)
    async with sessions() as db:
        lead=await agent_store.create_agent(db,owner_id=owner,name="Lead",provider="codex",make_lead=True)
        staff=await agent_store.create_agent(db,owner_id=owner,name="Worker",provider="codex",approval_mode="accept_edits")
        await service.change_member(db,owner,staff.id)
        staff.role_path=str(tmp_path)
        boss=await people.ensure_owner_person(db)
        chat=await thread_store.create_thread(db,owner_id=owner,agent_id=lead.id,provider="codex",workspace_path=str(tmp_path))
        turn=await thread_store.start_turn(db,chat.id,"Check the fixture",person_id=boss.id,origin="person",use_employee_role=True)
    rt=SimpleNamespace(session=sessions,owner_id=owner,adapters={"codex":CodexAdapter()},hub=ThreadHub())
    run=SimpleNamespace(thread_id=chat.id,turn_id=turn.id,agent_id=lead.id,owner_token=turn.owner_token,
        ctx=SimpleNamespace(approval_mode="ask",team=SimpleNamespace(team_id=lead.team_id,origin="person")))
    args={"agent_id":staff.id,"brief":"Inspect fixture","done_when":"Report result","project":"Fixture","read_only":read_only}
    job=await Jobs(rt).enqueue(run,args,reply="resume")
    assert job.task_id and job.approval_mode==expected


@pytest.mark.asyncio
async def test_gateway_requires_exact_owner_channel_and_immutable_input(sessions,owner,tmp_path,monkeypatch):
    import json
    import sqlite3
    from app.db.models import DirectTurnReceipt, FeishuLeadChat
    from app.feishu.lead_chat import signature
    from app.feishu.submissions import event_key
    from app.services.feature_tasks import direct_input_hash
    from app.team import feishu_assignment

    monkeypatch.setattr(settings,"STEWARD_STRICT_OWNER",owner)
    monkeypatch.setattr(employee_runtime,"native_enabled",lambda o,e: o==owner and e==staff.id)
    async with sessions() as db:
        lead=await agent_store.create_agent(db,owner_id=owner,name="Lead",provider="codex",make_lead=True)
        staff=await agent_store.create_agent(db,owner_id=owner,name="Worker",provider="codex",approval_mode="accept_edits")
        await service.change_member(db,owner,staff.id)
        staff.role_path=str(tmp_path)
        chat=await thread_store.create_thread(db,owner_id=owner,agent_id=lead.id,provider="codex",workspace_path=str(tmp_path))
        message="test the synthetic project"
        key=event_key({"app":"fake","event":"event"})
        turn=await thread_store.start_turn(db,chat.id,message,origin="person",use_employee_role=True)
        receipt=DirectTurnReceipt(owner_id=owner,event_key=key,thread_id=chat.id,turn_id=turn.id,
                                  input_sha256=direct_input_hash(message,[],[]))
        db.add(receipt)
        db.add(FeishuLeadChat(thread_id=chat.id,channel=signature(owner,"fake","our-tenant","our-owner","Project",chat.id),
                              selection="fixture",project="Project",isolated=False))
        await db.flush()
        cfg={"owner_id":owner,"steward_thread":chat.id,"steward":{"agent_id":lead.id,"app_id":"fake",
             "tenant_key":"our-tenant","open_id":"our-owner"},"project":"Project","state_dir":".",
             "_access_config_path":str(tmp_path/"fake.json")}
        monkeypatch.setattr(feishu_assignment,"configuration",lambda:cfg)
        with sqlite3.connect(tmp_path/"channel.sqlite3") as journal:
            journal.executescript("create table direct_requests(app,event,thread,body); create table owner_dm_sources(app,event,owner,tenant);")
            journal.execute("insert into direct_requests values(?,?,?,?)",("fake","event",chat.id,
                json.dumps({"prompt_text":message,"direct_event_key":key})))
            journal.execute("insert into owner_dm_sources values('fake','event','other','our-tenant')")
            journal.commit()
            assert await steward_review.direct_project_mode(db,owner,staff,turn.id,"ask")=="ask"
            journal.execute("update owner_dm_sources set owner='our-owner'")
            journal.commit()
            assert await steward_review.direct_project_mode(db,owner,staff,turn.id,"ask")=="accept_edits"
            receipt.input_sha256 = "0"*64
            await db.flush()
            assert await steward_review.direct_project_mode(db,owner,staff,turn.id,"ask")=="ask"
