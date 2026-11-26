from pathlib import Path

import pytest
from sqlalchemy import func, select
from app.db.models import Thread, WorkItem
from app.services import agent_store, feature_tasks


async def test_old_task_switches_only_after_private_binding_is_saved(sessions, owner, tmp_path, monkeypatch):
    from uuid import uuid4
    from app.db.models.feature import FeatureSession
    from app.services import thread_store

    root = tmp_path / "employee"
    root.mkdir()
    (root / "AGENTS.md").write_text("Role guide", encoding="utf-8")
    async with sessions() as db:
        agent = await agent_store.create_agent(db, owner_id=owner, name="Welder", provider="codex", model="gpt-fake")
        agent.role_path = str(root)
        card = await feature_tasks.route(db, owner_id=owner, agent_id=agent.id, project="test",
            event_key="one", title="check tests", text="Check the failing import", intent="independent")
        task_id = card["task_id"]
        old_folder = root / "任务" / str(task_id)
        old_folder.mkdir(parents=True)
        artifact = old_folder / "notes.txt"
        artifact.write_text("existing artifact", encoding="utf-8")
        opts = {"workspace_path": str(old_folder), "model": "gpt-fake"}
        old, _ = await feature_tasks.session_for(db, owner, task_id, **opts)
        old.provider_state, old.session_state = {"service_history": True}, "active"
        turn = await thread_store.start_turn(db, old.id, "Keep the original output.")
        receipt = await thread_store.append_event(db, old.id, "tool.completed", {"ok": True,
            "file_receipt": {"operation": "write_text", "scope": "task", "path": "notes.txt"},
            "output": "private tool output that must not be copied"}, turn_id=turn.id)
        decision = await thread_store.append_event(db, old.id, "approval.resolved", {"status": "allowed"}, turn_id=turn.id)
        await thread_store.finish_turn(db, turn.id, turn.owner_token, status="succeeded", result="Already prepared notes.txt")
        monkeypatch.setenv("SUPARAGENT_EMPLOYEE_NATIVE_IDS", f"{owner}/{agent.id}")
        new, busy = await feature_tasks.session_for(db, owner, task_id, **opts)
        item = await db.get(WorkItem, task_id)
        assert not busy and new.id != old.id and Path(new.workspace_path) == root
        assert item.thread_id == old.id
        assert (await db.get(FeatureSession, new.id)).previous_id == old.id
        assert (await feature_tasks.session_for(db, owner, task_id, **opts))[0].id == new.id
        context = await feature_tasks.context_for(db, owner, agent, new)
        assert "Keep the original output." in context and "Already prepared notes.txt" in context
        assert f"event #{receipt.id}" in context and f"event #{decision.id}" in context
        assert "private tool output" not in context
        runtime = await feature_tasks.project_runtime_for(db, owner, agent, new, "plan")
        assert runtime.root == root and not runtime.writable
        started = await thread_store.start_turn(db, new.id, "continue")
        state = {"employee_project": runtime.stamp, "codex_thread_id": str(uuid4()), "native_cwd": str(root)}
        assert not await feature_tasks.bind_native_session(db, owner, new.id, started.id, "wrong-token", state)
        assert await feature_tasks.bind_native_session(db, owner, new.id, started.id, started.owner_token, state)
        await db.refresh(item)
        assert item.thread_id == new.id and artifact.read_text(encoding="utf-8") == "existing artifact"
        assert (await feature_tasks.thread_handoff(db, owner, new.id)) == {"mode":"workbench", "thread_id":new.id}
        await thread_store.finish_turn(db, started.id, started.owner_token, status="succeeded")
        monkeypatch.delenv("SUPARAGENT_EMPLOYEE_NATIVE_IDS")
        legacy, _ = await feature_tasks.session_for(db, owner, task_id, **opts)
        assert legacy.id not in (old.id, new.id) and Path(legacy.workspace_path) == old_folder
        assert (await db.get(Thread, new.id)).provider_state["codex_thread_id"] == state["codex_thread_id"]


@pytest.mark.asyncio
async def test_replay_append_query_and_successor(db_session, tmp_path):
    db = db_session
    owner = "feature-owner"
    agent = await agent_store.create_agent(db, owner_id=owner, name="Welder")
    scope = dict(owner_id=owner, agent_id=agent.id, project="weld", title="strain", text="test")
    first = await feature_tasks.route(db, **scope, event_key="1")
    for key in ("1", "2", "3"):
        result = await feature_tasks.route(db, **scope, event_key=key)
        assert result["task_id"] == first["task_id"]
    await feature_tasks.route(db, **scope, event_key="4", intent="query")
    assert await db.scalar(select(func.count(Thread.id)).where(Thread.owner_id == owner)) == 0
    assert await db.scalar(select(func.count(WorkItem.id)).where(WorkItem.owner_id == owner)) == 1
    args = dict(model="example", workspace_path=str(tmp_path))
    thread, busy = await feature_tasks.session_for(db, owner, first["task_id"], **args)
    assert not busy
    again, _ = await feature_tasks.session_for(db, owner, first["task_id"], **args)
    assert again.id == thread.id
    thread.session_state = "broken"
    successor, _ = await feature_tasks.session_for(db, owner, first["task_id"], **args)
    assert successor.id != thread.id
    second = await feature_tasks.route(db, **scope, event_key="5", intent="independent", version="v2")
    assert second["task_id"] != first["task_id"]
    ambiguous = await feature_tasks.route(db, **scope, event_key="6", intent="append")
    assert set(ambiguous["candidates"]) == {first["task_id"], second["task_id"]}


@pytest.mark.asyncio
async def test_near_simultaneous_aliases_share_card(sessions, owner):
    import asyncio
    async with sessions() as db:
        agent = await agent_store.create_agent(db, owner_id=owner, name="Welder")
    async def receive(title, aliases, key):
        async with sessions() as db:
            result = await feature_tasks.route(db, owner_id=owner, agent_id=agent.id, project="weld",
                title=title, aliases=aliases, text=title, event_key=key)
            await db.commit()
            return result
    a, b = await asyncio.gather(receive("strain", ["deformation"], "a"), receive("deformation", ["strain"], "b"))
    assert a["task_id"] == b["task_id"]
    assert {a["action"], b["action"]} == {"created", "continued"}


from tests.services.thread_harness import owner, sessions  # noqa: E402,F401


@pytest.mark.asyncio
async def test_successor_recall_and_scope_after_reconnect(sessions, owner, tmp_path):
    from app.services import memory_recall, thread_store
    async with sessions() as db:
        welder = await agent_store.create_agent(db, owner_id=owner, name="Welder")
        viewer = await agent_store.create_agent(db, owner_id=owner, name="Viewer")
        welder.role_path = str(tmp_path)
        for agent, name, marker in [(welder, "strain", "alpha"), (welder, "pressure", "beta"),
                                    (viewer, "scene", "gamma")]:
            result = await feature_tasks.route(db, owner_id=owner, agent_id=agent.id, project="p",
                title=name, text=name, intent="independent", event_key=marker)
            task_id = result["task_id"]
            folder = tmp_path / "任务" / str(task_id)
            folder.mkdir(parents=True)
            thread, _ = await feature_tasks.session_for(db, owner, task_id, model="m", workspace_path=str(folder))
            await thread_store.append_event(db, thread.id, "assistant.message", {"text":f"material stress {marker}"})
            if marker == "alpha":
                first_task, first_thread, first_folder = task_id, thread.id, folder
        await db.commit()
    async with sessions() as db:
        prior = await db.get(Thread, first_thread)
        prior.session_state = "broken"
        successor, _ = await feature_tasks.session_for(db, owner, first_task, model="m", workspace_path=str(first_folder))
        recall = await memory_recall.build(db, owner_id=owner, agent_id=welder.id, thread_id=successor.id,
                                           message="material stress")
        assert [hit["thread_id"] for hit in recall.conversations] == [first_thread]
        assert "alpha" in recall.text and "beta" not in recall.text and "gamma" not in recall.text
        assert "strain" in await feature_tasks.context_for(db, owner, welder, successor)
        with pytest.raises(ValueError):
            await feature_tasks.context_for(db, owner, welder, prior)
        with pytest.raises(ValueError):
            await feature_tasks.context_for(db, owner, viewer, successor)


def test_confirmed_facts_match_role_and_version(tmp_path):
    import json
    roles = []
    for name in ("welder", "viewer"):
        folder = tmp_path / name / "资料"
        folder.mkdir(parents=True)
        values = [{"fact": f"{name}/{v}", "source": "approved reference", "version":v} for v in ("v1","v2","*")]
        (folder / "领域事实.json").write_text(json.dumps(values), encoding="utf-8")
        roles.append(folder.parent)
    facts = "\n".join(feature_tasks.confirmed_facts(roles[0], "v1"))
    assert "welder/v1" in facts and "welder/*" in facts
    assert "welder/v2" not in facts and "viewer" not in facts


@pytest.mark.asyncio
async def test_concurrent_submission_reserves_only_one_turn(sessions, owner, tmp_path):
    import asyncio
    from app.db.models import Turn
    from app.services import thread_store
    from app.team import service
    async with sessions() as db:
        lead = await agent_store.create_agent(db, owner_id=owner, name="Steward")
        lead.role_path = str(tmp_path)
        specialist = await agent_store.create_agent(db, owner_id=owner, name="Welder")
        await service.set_lead(db, owner, lead.id)
        await service.change_member(db, owner, specialist.id)
        receipt = await feature_tasks.route(db, owner_id=owner, agent_id=specialist.id, project="weld",
            event_key="dm-1", title="strain", text="review")
        task = receipt["task_id"]
        thread, _ = await feature_tasks.session_for(db, owner, task, model="codex", workspace_path=str(tmp_path))
        await feature_tasks.dispatch(db, owner_id=owner, steward_id=lead.id, task_id=task, event_key="dm-1",
                                     thread_id=thread.id)
        await db.commit()
    async def submit():
        async with sessions() as db:
            try:
                turn = await thread_store.start_turn(db, thread.id, "review", feature_event_key="dm-1")
                return (turn.id, True)
            except thread_store.TurnAlreadyStarted as replay:
                return (replay.turn.id, False)
    first, second = await asyncio.gather(submit(), submit())
    assert first[0] == second[0] and first[1] != second[1]
    async with sessions() as db:
        assert await db.scalar(select(func.count(Turn.id)).where(Turn.thread_id == thread.id)) == 1
        with pytest.raises(feature_tasks.DispatchConflict, match="different input"):
            await thread_store.start_turn(db, thread.id, "changed", feature_event_key="dm-1")
