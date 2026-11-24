from uuid import uuid4

from app.db.models import Thread
from app.services import agent_store, feature_tasks
from app.team import service
from tests.services.thread_harness import owner, sessions  # noqa: F401


async def test_steward_resume_uses_recorded_provider_and_authorized_thread(sessions, owner, tmp_path):
    native_id = str(uuid4())
    async with sessions() as db:
        lead = await agent_store.create_agent(db, owner_id=owner, name="lead", provider="codex")
        await service.set_lead(db, owner, lead.id)
        thread = Thread(owner_id=owner, agent_id=lead.id, provider="codex", kind="chat",
                        workspace_path=str(tmp_path), session_state="active",
                        provider_state={"codex_thread_id":native_id, "steward_deepseek": True})
        db.add(thread)
        await db.commit()
        result = await feature_tasks.thread_handoff(db, owner, thread.id)
        assert result == {"mode":"workbench", "thread_id":thread.id}
        assert await feature_tasks.thread_handoff(db, "foreign", thread.id) is None
        assert await feature_tasks.thread_handoff(db, owner, thread.id, task_id=42) is None
        thread.provider_state = {"codex_thread_id":native_id}
        await db.commit()
        assert (await feature_tasks.thread_handoff(db, owner, thread.id))["resume_command"] == "codex resume " + native_id
        replacement = await agent_store.create_agent(db, owner_id=owner, name="new lead", provider="codex")
        await service.set_lead(db, owner, replacement.id)
        thread.provider_state = {"codex_thread_id":native_id, "steward_deepseek":True}
        await db.commit()
        assert await feature_tasks.thread_handoff(db, owner, thread.id) is None
