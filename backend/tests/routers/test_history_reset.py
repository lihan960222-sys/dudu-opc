from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from app.auth.policy import Principal
from app.core.config import settings
from app.routers.history_reset import router
from app.services import history_reset_control as control
from tests.routers.test_agents_api import app_for, client_for
from tests.services.thread_harness import cli, owner, rt, sessions  # noqa: F401


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "directory", lambda: tmp_path)
    monkeypatch.setattr(control, "available", lambda: True)
    preview = dict(conversations=3, turns=7, tasks=2, jobs=2, approvals=0, busy=False, revision="v1")
    monkeypatch.setattr(control.history_reset, "preview", lambda: preview.copy())
    clear = Mock()
    monkeypatch.setattr(control.history_reset, "clear", clear)
    return preview, clear


def test_repeated_confirmation_claimed_once_and_status_survives_reload(host):
    with ThreadPoolExecutor(2) as pool:
        first, retry = pool.map(control.enqueue, ["v1", "v1"])
    assert first == retry and first["state"] == "queued"
    assert control.worker("check", first["id"]) and control.worker("apply", first["id"])
    assert not control.worker("apply", first["id"])
    host[1].assert_called_once_with("v1")
    result = control.view()["operation"]
    assert result["id"] == first["id"] and result["state"] == "succeeded"


def test_new_work_or_changed_revision_prevents_removal(host):
    state, clear = host
    state["busy"] = True
    with pytest.raises(control.Conflict):
        control.enqueue("v1")
    state["busy"] = False
    with pytest.raises(control.Conflict):
        control.enqueue("old")
    old = control.enqueue("v1")["id"]
    state["revision"] = "v2"
    assert not control.worker("check", old)
    assert control.view()["operation"]["state"] == "failed"
    new = control.enqueue("v2")["id"]
    assert not control.worker("fail", old)
    assert control.worker("check", new)
    clear.assert_not_called()


def test_stale_or_crashed_worker_never_replays_request(host, monkeypatch):
    old = control.enqueue("v1")["id"]
    monkeypatch.setattr(control.time, "time", lambda: 9999999999)
    assert not control.worker("check", old)
    new = control.enqueue("v1")["id"]
    with control.transaction() as (db, _):
        db.execute("UPDATE operation SET state='running'")
    assert not control.worker("apply", new)
    host[1].assert_not_called()


async def test_only_owner_browser_can_preview_and_confirm(host, rt, monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_EXECUTION_ENABLED", True)
    app = app_for(rt)
    app.include_router(router, prefix="/api/v1/staff")
    who = Principal(kind="browser", role="member", person_id=8, session_id=11)

    @app.middleware("http")
    async def principal(request, next_handler):
        request.state.auth = who
        return await next_handler(request)

    path = "/api/v1/staff/history-reset"
    body = {"revision": "v1", "confirmation": "清空历史记录"}
    async with client_for(app) as client:
        assert (await client.get(path)).status_code == 403
        assert (await client.post(path, json=body)).status_code == 403
        who = Principal(kind="browser", role="owner", person_id=1, session_id=11)
        assert (await client.get(path)).json()["preview"]["turns"] == 7
        assert (await client.post(path, json={**body, "confirmation":"查看"})).status_code == 422
        assert (await client.post(path, json={**body, "revision":"old"})).status_code == 409
        response = await client.post(path, json=body)
        assert response.status_code == 202 and response.json()["operation"]["state"] == "queued"
        host[1].assert_not_called()
