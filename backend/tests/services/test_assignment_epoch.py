import asyncio

from app.db.models import FeatureScope, WorkItem
from app.services import agent_store, feature_tasks
from app.team import service
from tests.services.thread_harness import owner, sessions  # noqa: F401


async def test_replayed_corrections_change_the_owner_once(sessions, owner):
    async with sessions() as db:
        await agent_store.create_agent(db, owner_id=owner, name="Lead", make_lead=True)
        old = await agent_store.create_agent(db, owner_id=owner, name="First")
        new = await agent_store.create_agent(db, owner_id=owner, name="Second")
        for employee in (old,new):
            await service.change_member(db, owner, employee.id)
        initial = await feature_tasks.route(db,owner_id=owner,agent_id=old.id,event_key="create",
            project="test",title="Import",text="preserve this goal")
        await db.commit()

    async def correct():
        async with sessions() as db:
            out = await feature_tasks.reassign(db,initial["task_id"],1,new.id,"owner-correction",owner_id=owner)
            await db.commit()
            return out
    a,b = await asyncio.gather(correct(),correct())
    assert a == b and a["assignment_epoch"] == 2
    async with sessions() as db:
        card = await db.get(WorkItem,initial["task_id"])
        assert card.agent_id == new.id and card.description == "preserve this goal"
        assert (await db.get(FeatureScope,card.id)).assignment_epoch == 2
