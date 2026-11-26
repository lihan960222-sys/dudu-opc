"""Through the real task intake and runtime, with an in-process recording engine."""
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.models import Event
from app.services import agent_store, codex_adapter, feature_tasks, local_claude_runner, role_workspace, steward_deepseek
from app.services.provider_adapter import Capabilities, ProviderStatus, TurnOutcome
from app.services.thread_runtime import ThreadRuntime
from app.services.thread_hub import ThreadHub
from app.team import service
from tests.services.thread_harness import finished, owner, sessions


class RecordingEngine:
    capabilities = Capabilities(project_workspace=True, tools=True, mcp=True, approvals=True, resume=True)
    custom_models = True

    def __init__(self, engine):
        self.key, self.calls = engine, []

    def new_session_id(self):
        return None

    async def status(self):
        return ProviderStatus(ready=True)

    async def run_turn(self, ctx, sink, gate):
        self.calls.append((ctx.model_vendor, ctx.model, ctx.effort, ctx.project_runtime.employee_id))
        await sink.emit("tool.completed", {"name":"fixture_project_read", "result":"fixture result"})
        return TurnOutcome(status="succeeded", model=ctx.model, result="fixture result")


@pytest.mark.asyncio
@pytest.mark.parametrize("engine,vendor,model,effort", [
    ("codex","deepseek","deepseek-flash","high"),
    ("fixture_engine","acme","fixture-2","low"),
])
async def test_worker_profile_reaches_selected_provider(sessions, owner, tmp_path, monkeypatch, engine, vendor, model, effort):
    monkeypatch.setattr(local_claude_runner, "workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(settings, "TEAM_ENABLED", False)
    monkeypatch.setattr(codex_adapter, "employee_codex_status", AsyncMock(return_value=ProviderStatus(ready=True)))
    monkeypatch.setattr(steward_deepseek, "employee_models", lambda *_: ["deepseek-flash"])
    adapter = RecordingEngine(engine)
    rt = ThreadRuntime(hub=ThreadHub(), adapters={engine:adapter}, status_ttl=0, _session_factory=sessions, owner_id=owner)
    try:
        async with sessions() as db:
            lead = await agent_store.create_agent(db, owner_id=owner, name="Lead", provider="codex", make_lead=True)
            employee = await agent_store.create_agent(db, owner_id=owner, name="Worker", provider=engine, model=model,
                effort=effort, model_vendor=vendor)
            await service.change_member(db, owner, employee.id)
            folder = tmp_path / "employee"
            folder.mkdir()
            role_workspace.initialize(folder, model, "Inspect the fixture")
            employee.role_path = str(folder)
            monkeypatch.setenv("SUPARAGENT_EMPLOYEE_NATIVE_IDS", f"{owner}/{employee.id}")
            card = (await feature_tasks.route(db, owner_id=owner, agent_id=employee.id, project="P", event_key="go",
                title="Routing fixture", text="Read fixture"))["task_id"]
            thread, _ = await feature_tasks.session_for(db, owner, card, model=model, workspace_path=str(folder))
            await feature_tasks.dispatch(db, owner_id=owner, steward_id=lead.id, task_id=card, event_key="go", thread_id=thread.id)
            await db.commit()
        turn = await rt.send_turn(thread.id, "Read fixture", feature_event_key="go", internal=True)
        await finished(sessions, thread.id)
        assert adapter.calls == [(vendor, model, effort, employee.id)]
        async with sessions() as db:
            result = await db.scalar(select(Event.data).where(Event.turn_id == turn.id, Event.type == "model.receipt"))
            assert result["requested_model"] == result["confirmed_model"] == model
            assert result["model_vendor"] == vendor and result["effort"] == effort
    finally:
        await rt.shutdown()
