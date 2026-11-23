from types import SimpleNamespace as Row
import pytest
from app.core.config import settings
from app.team import policy, service


def test_only_authenticated_lead_turn_dispatches(monkeypatch):
    monkeypatch.setattr(settings, "STEWARD_STRICT_OWNER", "mine")
    team = Row(owner_id="mine", lead_agent_id=1)
    turn = Row(thread_id=7)
    thread = Row(id=7, owner_id="mine", agent_id=1)
    service.require_dispatcher(team, Row(id=1), turn, thread)
    for caller, origin in [(2, thread), (1, Row(id=7, owner_id="mine", agent_id=2)),
                           (1, Row(id=8, owner_id="mine", agent_id=1))]:
        with pytest.raises(service.TeamError, match="steward"):
            service.require_dispatcher(team, Row(id=caller), turn, origin)
    specialist = policy.TurnTeam(1, 2, False, False, strict_hierarchy=True)
    assert policy.automatic("mcp__team__delegate", "ask", specialist, {})[0] == "denied"


@pytest.mark.asyncio
async def test_specialist_chat_cannot_start_unscoped_turn(sessions, owner, tmp_path, monkeypatch):
    from app.services import agent_store, thread_store
    from app.services.thread_hub import ThreadHub
    from app.services.thread_runtime import ThreadRuntime, ProviderUnavailable
    from tests.team.test_collaboration import Engine
    monkeypatch.setattr(settings, "STEWARD_STRICT_OWNER", owner)
    async with sessions() as db:
        lead = await agent_store.create_agent(db, owner_id=owner, name="Steward")
        specialist = await agent_store.create_agent(db, owner_id=owner, name="Welder")
        await service.set_lead(db, owner, lead.id)
        await service.change_member(db, owner, specialist.id)
        chat = await thread_store.create_thread(db, owner_id=owner, agent_id=specialist.id, provider="claude",
                                                workspace_path=str(tmp_path))
    engine = Engine()
    rt = ThreadRuntime(hub=ThreadHub(), adapters={"claude":engine}, owner_id=owner, _session_factory=sessions)
    with pytest.raises(ProviderUnavailable, match="scoped steward task"):
        await rt.send_turn(chat.id, "read notes")
    assert engine.contexts == []


from tests.services.thread_harness import sessions, owner  # noqa: E402,F401
