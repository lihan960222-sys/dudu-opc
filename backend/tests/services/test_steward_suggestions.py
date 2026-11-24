import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.auth.policy import required_scope
from app.db.base import Base
from app.db.engine import create_app_async_engine
from app.db.models import AppSetting, StewardSuggestion, WorkColumn, WorkItem, WorkItemEvent
from app.routers.steward_suggestions import router
from app.routers.threads import get_runtime
from app.services import agent_store, steward_suggestions, work_planner
from app.services.local_access_policy import require_local_access


@pytest.fixture
async def state(tmp_path):
    engine = create_app_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'opc.db'}")
    async with engine.begin() as cx:
        await cx.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        lead = await agent_store.create_agent(db, owner_id="boss", name="管家", make_lead=True)
        board = await work_planner.default_board(db, "boss")
        cols = {c.key: c.id for c in await db.scalars(select(WorkColumn).where(WorkColumn.board_id == board.id))}
        item = await work_planner.create_item(db, "boss", dict(board_id=board.id, column_id=cols["waiting"],
                                               title="导出工具", description="等待接口说明"))
    app = FastAPI()
    app.include_router(router)
    rt = SimpleNamespace(session=sessions, owner_id="boss")
    app.dependency_overrides[get_runtime] = lambda: rt
    app.dependency_overrides[require_local_access] = lambda: None
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield sessions, client, rt, item.id, lead.id, cols
    finally:
        await engine.dispose()


BASE = "/steward/suggestions"


async def test_two_hour_patrol_survives_restart_and_list_reads(state):
    sessions, client, _, first, _, cols = state
    start = datetime(2030, 1, 1, tzinfo=UTC)
    async def tick(at):
        async with sessions() as db:
            return await steward_suggestions.scan(db, "boss", now=at)

    assert await tick(start) == 7200
    async with sessions() as db:
        board = await work_planner.default_board(db, "boss")
        second = await work_planner.create_item(db, "boss", dict(board_id=board.id,
            column_id=cols["waiting"], title="Second observation"))
    assert await tick(start + timedelta(seconds=7199)) == 1
    assert [s["task_id"] for s in (await client.get(BASE)).json()["items"]] == [first]
    assert await asyncio.gather(*(tick(start + timedelta(hours=2)) for _ in range(2))) == [7200,7200]
    assert {s["task_id"] for s in (await client.get(BASE)).json()["items"]} == {first,second.id}
    # Coming back after a long outage does one review and starts a new interval.
    assert await tick(start + timedelta(days=1)) == 7200
    assert await tick(start + timedelta(days=1,seconds=10)) == 7190


async def test_first_upgrade_uses_last_known_legacy_observation(state):
    sessions, client, *_ = state
    await scan(sessions)
    async with sessions() as db:
        old = await db.scalar(select(StewardSuggestion.created_at))
        await db.delete(await db.get(AppSetting,"internal.steward.patrol.boss"))
        await db.commit()
    async with sessions() as db:
        assert await steward_suggestions.scan(db,"boss",now=old+timedelta(minutes=30)) == 5400
    assert len((await client.get(BASE)).json()["items"]) == 1


async def scan(sessions):
    async with sessions() as db:
        await steward_suggestions.scan(db, "boss")


async def test_discovery_is_passive_then_owner_can_choose_one_followup(state):
    sessions, client, rt, item_id, lead_id, cols = state
    await asyncio.gather(scan(sessions), scan(sessions))
    rows = (await client.get(BASE)).json()["items"]
    assert len(rows) == 1
    proposal = rows[0]
    assert proposal["task_id"] == item_id
    assert [o["id"] for o in proposal["options"]] == ["brief", "followup"]
    async with sessions() as db:
        item = await db.get(WorkItem, item_id)
        assert item.revision == 1 and item.column_id == cols["waiting"]
        assert await db.scalar(select(func.count(WorkItem.id))) == 1
    path = f"{BASE}/{proposal['id']}"
    req = dict(choice="approve", option="followup", revision=proposal["revision"])
    rt.owner_id = "other"
    assert (await client.get(path)).status_code == 404
    assert (await client.post(path + "/decision", json=req)).status_code == 404
    rt.owner_id = "boss"
    first, second = await asyncio.gather(*(client.post(path + "/decision", json=req) for _ in range(2)))
    assert first.status_code == second.status_code == 200
    assert first.json()["followup_task_id"] == second.json()["followup_task_id"]
    assert first.json()["state"] == "approved" and first.json()["report"]
    assert (await client.post(path + "/decision", json=dict(req, option="brief"))).status_code == 409
    async with sessions() as db:
        followup = await db.get(WorkItem, first.json()["followup_task_id"])
        assert followup.agent_id == lead_id and f"#{item_id}" in followup.description
        assert await db.scalar(select(func.count(WorkItem.id))) == 2
        assert await db.scalar(select(func.count(WorkItemEvent.id)).where(
            WorkItemEvent.work_item_id == item_id, WorkItemEvent.kind == "steward.approved")) == 1
    await scan(sessions)
    assert (await client.get(BASE)).json()["items"] == []


@pytest.mark.parametrize("choice", ["approve", "defer", "dismiss"])
async def test_brief_and_deferrals_do_not_start_work_or_repeat(state, choice):
    sessions, client, _, item_id, *_ = state
    await scan(sessions)
    p = (await client.get(BASE)).json()["items"][0]
    url = f"{BASE}/{p['id']}/decision"
    data = {"choice": choice, "revision": p["revision"]}
    decided = await client.post(url, json=data)
    assert decided.status_code == 200
    assert decided.json()["state"] == {"approve":"approved","defer":"deferred","dismiss":"dismissed"}[choice]
    if choice == "approve":
        assert "等待接口说明" in decided.json()["report"]
    assert (await client.post(url, json=data)).json() == decided.json()
    await scan(sessions)
    assert (await client.get(BASE)).json()["items"] == []
    async with sessions() as db:
        assert await db.scalar(select(func.count(WorkItem.id))) == 1


async def test_review_after_the_task_changed_cannot_apply_a_stale_card(state):
    sessions, client, _, item_id, _, cols = state
    await scan(sessions)
    p = (await client.get(BASE)).json()["items"][0]
    async with sessions() as db:
        await work_planner.update_item(db, "boss", item_id, 1, {"column_id":cols["done"]})
    route = f"{BASE}/{p['id']}"
    assert (await client.post(route + "/decision", json={"choice":"approve","revision":1})).status_code == 409
    assert (await client.get(route)).json()["state"] == "expired"
    assert (await client.get(BASE)).json()["items"] == []


def test_only_management_device_or_admin_can_review():
    assert required_scope("GET", "/api/v1" + BASE) == "manage"
    assert required_scope("POST", "/api/v1" + BASE + "/1/decision") == "manage"


async def test_boss_can_revisit_deferred_without_repeating_the_push(state):
    sessions, client, *_ = state
    await scan(sessions)
    original = (await client.get(BASE)).json()["items"][0]
    url = f"{BASE}/{original['id']}/decision"
    deferred = (await client.post(url, json={"choice":"defer", "revision":1})).json()
    assert (await client.get(BASE)).json()["items"] == []
    assert (await client.get(BASE + "?state=deferred")).json()["items"] == [deferred]
    approved = await client.post(url, json={"choice":"approve", "revision":deferred["revision"]})
    assert approved.status_code == 200 and approved.json()["state"] == "approved"
