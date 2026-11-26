import pytest

from app.auth.policy import Principal
from app.core.config import settings
from app.db.models import Agent, AgentJob
from app.routers.staff import router
from app.services import agent_store, employee_runtime, feature_tasks, role_workspace, thread_store
from tests.routers.test_agents_api import app_for, client_for
from tests.services.thread_harness import cli, owner, rt, sessions  # noqa: F401


async def test_owner_switch_applies_to_one_existing_employee_and_same_work_item(sessions, owner, rt, cli, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_EXECUTION_ENABLED", True)
    paths = [tmp_path / "jensen", tmp_path / "peer"]
    for path in paths:
        path.mkdir()
        role_workspace.initialize(path, "gpt-fake", "Work within this project.")
    async with sessions() as db:
        first = await agent_store.create_agent(db, owner_id=owner, name="Jensen", provider="codex", model="gpt-fake")
        peer = await agent_store.create_agent(db, owner_id=owner, name="Peer", provider="codex", model="gpt-fake")
        first.role_path, peer.role_path = map(str, paths)
        first.approval_mode = "plan"
        await db.commit()
    monkeypatch.setenv("SUPARAGENT_EMPLOYEE_NATIVE_IDS", f"{owner}/{peer.id}")
    live = Principal(kind="browser", role="owner", person_id=3, session_id=72)
    app = app_for(rt)
    app.include_router(router, prefix="/api/v1")

    @app.middleware("http")
    async def auth(request, next_handler):
        request.state.auth = live
        return await next_handler(request)

    endpoint = f"/api/v1/staff/{first.id}/project-terminal"
    other_endpoint = f"/api/v1/staff/{peer.id}/project-terminal"
    async with client_for(app) as client:
        assert (await client.get(endpoint)).json()["enabled"] is False
        assert (await client.get(other_endpoint)).json()["enabled"] is True
        for live in (Principal(kind="device",session_id=71), Principal(kind="browser",role="member",person_id=4,session_id=77),
                     Principal(kind="browser",session_id=84), Principal(kind="browser", role="owner", session_id=85)):
            assert (await client.get(endpoint)).status_code == 403
            assert (await client.put(endpoint,json={"enabled":True})).status_code == 403
        live = Principal(kind="browser", role="owner", person_id=3, session_id=72)
        assert (await client.put(endpoint,json={"enabled":"true"})).status_code == 422
        assert (await client.put(endpoint,json={"enabled":True})).json()["enabled"] is True
        assert (await client.get(endpoint)).json()["enabled"] is True
        assert not employee_runtime.native_enabled(owner+"other",first.id)
        async with sessions() as db:
            first = await db.get(Agent, first.id)
            assert first.approval_mode == "plan" and not first.team_workspaces_readable
            card = await feature_tasks.route(db, owner_id=owner,agent_id=first.id,project="demo",event_key="gate-1",
                                             title="Project test",text="Check the files",intent="independent")
            task = card["task_id"]
            old = paths[0] / "任务" / str(task)
            old.mkdir(parents=True)
            options = {"workspace_path":str(old), "model":"gpt-fake"}
            thread, _ = await feature_tasks.session_for(db, owner, task, **options)
            assert thread.workspace_path == str(paths[0])
            assert (await feature_tasks.session_for(db, owner, task, **options))[0].id == thread.id
            native = await feature_tasks.project_runtime_for(db, owner, first, thread, "accept_edits")
            assert not native.writable and native.readable_roots == ()
            job = AgentJob(owner_id=owner, assignee_agent_id=first.id, requester_agent_id=peer.id,
                requester_thread_id=thread.id, requester_turn_id=1, depth=1, brief="test", brief_hash="x",
                mode="work", task_id=task, status="dispatched")
            db.add(job)
            await db.commit()
        assert (await client.put(endpoint,json={"enabled":False})).status_code == 409
        assert (await client.get(endpoint)).json()["enabled"] is True
        async with sessions() as db:
            (await db.get(AgentJob,job.id)).status="completed"
            await db.commit()
            running = await thread_store.start_turn(db, thread.id, "Synthetic active feature turn")
        assert (await client.put(endpoint,json={"enabled":False})).status_code == 409
        async with sessions() as db:
            await thread_store.finish_turn(db,running.id,running.owner_token,status="succeeded")
        assert (await client.put(endpoint,json={"enabled":False})).json()["enabled"] is False
        async with sessions() as db:
            successor, _ = await feature_tasks.session_for(db,owner,task,**options)
            assert successor.workspace_path == str(old) and successor.id != thread.id
        assert (await client.get(other_endpoint)).json()["enabled"] is True
        assert (await client.put(other_endpoint,json={"enabled":False})).json()["enabled"] is False
        assert not employee_runtime.native_enabled(owner,peer.id)
        async with sessions() as db:
            (await db.get(Agent,first.id)).role_path = str(paths[1])
            await db.commit()
        assert (await client.put(endpoint,json={"enabled":True})).status_code == 422
        async with sessions() as db:
            (await db.get(Agent,first.id)).owner_id = owner+"other"
            await db.commit()
        assert (await client.put(endpoint,json={"enabled":True})).status_code == 404


def test_corrupt_or_linked_preferences_cannot_reenable_the_host_default(tmp_path, monkeypatch):
    import os

    monkeypatch.setattr(settings, "SQLITE_PATH", str(tmp_path / "isolated.sqlite"))
    monkeypatch.setenv("SUPARAGENT_EMPLOYEE_NATIVE_IDS", "host/4")
    employee_runtime.save_native_enabled("host", 4, False)
    saved = next((tmp_path / "employee-settings" / "4").glob("*.json"))
    saved.write_text("corrupt")
    with pytest.raises(employee_runtime.BoundaryUnavailable):
        employee_runtime.native_enabled("host", 4)
    saved.write_text("true\n")
    os.link(saved, tmp_path / "other.json")
    with pytest.raises(employee_runtime.BoundaryUnavailable):
        employee_runtime.native_enabled("host", 4)
