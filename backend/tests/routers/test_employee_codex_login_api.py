from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.core.config import settings
from app.db.models import Agent
from app.routers.agents import get_runtime
from app.services import agent_store, cli_probe, codex_adapter, employee_runtime
from app.services.codex_login import CodexLogin
from tests.auth.test_auth_api import app, auth_settings, client, factory, login, pairing_code, set_owner


@pytest.fixture
async def roster(app, factory, monkeypatch, tmp_path):
    owner = "login-test-" + uuid4().hex
    foreign = owner + "-other"
    monkeypatch.setattr(settings, "LOCAL_EXECUTION_ENABLED", True)
    monkeypatch.setattr(settings, "SQLITE_PATH", str(tmp_path / "data" / "test.sqlite"))
    monkeypatch.delenv("SUPARAGENT_EMPLOYEE_RUNTIME_ROOT", raising=False)
    exe = tmp_path / "codex.exe"
    exe.touch()
    monkeypatch.setattr(codex_adapter, "resolve_employee_executable", lambda: exe)
    monkeypatch.setattr(employee_runtime.host_exec, "employee_host_blocker", lambda: None)
    async with factory() as db:
        one, two, other = [await agent_store.create_agent(db, owner_id=o, name=n, provider="codex", model="gpt-5")
                           for o,n in ((owner,"One"), (owner,"Two"), (foreign,"Other"))]
        wrong = await agent_store.create_agent(db, owner_id=owner, name="Wrong", provider="claude")
    states = {}
    app.dependency_overrides[get_runtime] = lambda: SimpleNamespace(
        owner_id=owner, session=factory, employee_codex_logins=states, _closing=False, adapters={"codex":object()})
    starts = []
    async def fake_start(self, owner, executable, *, employee_env):
        starts.append((owner, executable, employee_env))
        self.flow = {"owner":owner, "id":uuid4().hex, "phase":"running", "user_code":"AB12-CD34"}
        return self.view(owner, self.flow["id"])
    monkeypatch.setattr(CodexLogin, "start", fake_start)
    try:
        yield owner, [one.id, two.id, other.id, wrong.id], starts
    finally:
        async with factory() as db:
            await db.execute(delete(Agent).where(Agent.owner_id.in_((owner, foreign))))
            await db.commit()


async def test_explicit_owner_login_is_bound_to_employee_and_browser_with_no_global_auth(app, factory, roster):
    await set_owner(factory)
    owner, (one, two, other, wrong), starts = roster
    account = lambda employee: f"/api/v1/staff/{employee}/openai"
    async with client(app) as admin, client(app) as second, client(app) as phone:
        session = await login(admin)
        csrf = {"X-CSRF-Token":session["csrf_token"]}
        second_session = await login(second)
        code = await pairing_code(admin, session["csrf_token"], scope="admin")
        token = (await phone.post("/api/v1/auth/pair", json={"code":code})).json()["token"]
        bearer = {"Authorization":"Bearer " + token}
        assert (await phone.get(account(one), headers=bearer)).status_code == 403
        assert (await phone.post(account(one)+"/login", headers=bearer)).status_code == 403
        assert (await admin.get(account(other))).status_code == 404
        assert (await admin.post(account(wrong)+"/login", headers=csrf)).status_code == 409
        assert (await admin.post(account(one)+"/login")).status_code == 403
        injected = await admin.post(account(one)+"/login", headers=csrf, json={"profile":"someone-else","api_key":"secret"})
        assert injected.status_code == 422 and "secret" not in injected.text
        info = await admin.get(account(one))
        assert info.status_code == 200 and info.json() == {"signed_in":False}
        assert info.headers["cache-control"] == "no-store" and starts == []
        first = await admin.post(account(one)+"/login", headers=csrf)
        assert first.status_code == 202 and first.headers["cache-control"] == "no-store"
        first_id = first.json()["id"]
        second_path = account(two)+"/login"
        assert (await admin.get(second_path+"/"+first_id)).status_code == 404
        assert (await admin.post(second_path, headers=csrf)).status_code == 202
        first_path = account(one)+"/login/"+first_id
        assert (await admin.get(first_path)).json()["user_code"] == "AB12-CD34"
        assert (await admin.get(account(one))).json()["login"]["id"] == first_id
        assert "login" not in (await second.get(account(one))).json()
        assert (await second.get(first_path)).status_code == 404
        assert (await second.delete(first_path, headers={"X-CSRF-Token":second_session["csrf_token"]})).status_code == 404
        assert (await admin.delete(first_path, headers=csrf)).status_code == 204
        assert len(starts) == 2
        for employee, (_, _, env) in zip((one,two),starts):
            assert env["CODEX_HOME"] == str(employee_runtime.profile_dir(owner, employee, "openai"))
            assert env["HOME"] == env["USERPROFILE"] == env["CODEX_HOME"]
            assert "OPENAI_API_KEY" not in env


async def test_saved_profile_status_uses_exact_employee_home_and_never_returns_cli_output(app, factory, roster, monkeypatch):
    await set_owner(factory)
    owner, (one, two, *_), starts = roster
    home = employee_runtime.profile_dir(owner, one, "openai")
    home.mkdir(parents=True)
    (home / "auth.json").write_text("private-saved-token")
    probe = AsyncMock(return_value=cli_probe.ProbeRun(0, "", "Logged in using ChatGPT\nprivate-email@example.com"))
    monkeypatch.setattr(cli_probe, "run", probe)
    async with client(app) as admin:
        await login(admin)
        for employee, signed_in in ((one,True),(two,False)):
            result = await admin.get(f"/api/v1/staff/{employee}/openai")
            assert result.status_code == 200 and result.json() == {"signed_in":signed_in}
            assert result.headers["cache-control"] == "no-store"
    probe.assert_awaited_once()
    assert probe.await_args.kwargs["env"]["CODEX_HOME"] == str(home)
    assert probe.await_args.kwargs["cwd"] == str(home) and not starts


async def test_login_refuses_personal_home_overlap_even_with_an_owned_employee(app, factory, roster, monkeypatch):
    await set_owner(factory)
    owner, (one, *_), starts = roster
    home = employee_runtime.profile_dir(owner, one, "openai")
    monkeypatch.setenv("CODEX_HOME", str(home))
    async with client(app) as admin:
        session = await login(admin)
        result = await admin.post(f"/api/v1/staff/{one}/openai/login", headers={"X-CSRF-Token":session["csrf_token"]})
        assert result.status_code == 409 and not starts and not home.exists()
