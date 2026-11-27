from app.auth.policy import Principal
from app.core.config import settings
from app.routers.staff import router
from app.services import agent_store, shared_library
from tests.routers.test_agents_api import app_for, client_for
from tests.services.thread_harness import cli, owner, rt, sessions  # noqa: F401
from tests.services.test_shared_library import spec


async def test_owner_import_and_project_pin(sessions, owner, rt, cli, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_EXECUTION_ENABLED", True)
    monkeypatch.setattr(settings, "SQLITE_PATH", str(tmp_path / "private" / "app.db"))
    monkeypatch.setattr(shared_library, "_download", lambda _: b"pass")
    async with sessions() as db:
        agent = await agent_store.create_agent(db, owner_id=owner, name="Worker", provider="codex", model="gpt-fake")
        root = tmp_path / "role"
        root.mkdir()
        agent.role_path = str(root)
        await db.commit()
    app = app_for(rt)
    app.include_router(router, prefix="/api/v1")
    who = Principal(kind="browser", role="member", person_id=6, session_id=12)

    @app.middleware("http")
    async def auth(request, next_handler):
        request.state.auth = who
        return await next_handler(request)

    item = spec(b"pass", org="unverified").model_dump()
    endpoint = "/api/v1/staff/shared-library"
    project = f"{endpoint}/{agent.id}"
    async with client_for(app) as client:
        assert (await client.post(endpoint, json={"release":item,"approval":"publisher"})).status_code == 403
        who = Principal(kind="browser",role="owner",person_id=4,session_id=12)
        assert (await client.post(endpoint, json={"release":item})).status_code == 422
        assert (await client.post(endpoint, json={"release":item,"approval":"version"})).status_code == 200
        response = await client.put(project, json={"name":item["name"],"version":item["version"]})
        assert response.status_code == 200, response.text
        assert (await client.get(project)).json()["pins"][0]["sha256"] == item["sha256"]
        assert (await client.put(project,json={"name":item["name"],"version":None})).json()["pins"] == []
