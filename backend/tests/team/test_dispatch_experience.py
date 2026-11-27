import json

from app.db.models import AgentJob, AgentJobEvent, WorkItem
from app.services import agent_store, assignment_context, feature_tasks, thread_store
from tests.services.thread_harness import owner, sessions  # noqa: F401


async def test_lead_recalls_verified_examples_only_from_this_conversation(sessions, owner, tmp_path):
    async with sessions() as db:
        lead = await agent_store.create_agent(db, owner_id=owner, name="大嘴", make_lead=True)
        employee = await agent_store.create_agent(db, owner_id=owner, name="工程师")
        from app.team import service
        await service.change_member(db, owner, employee.id)

        async def conversation():
            return await thread_store.create_thread(db, owner_id=owner, agent_id=lead.id, provider="codex",
                                                   workspace_path=str(tmp_path))

        chat, other = await conversation(), await conversation()
        async def job(thread, title, *, state="files_verified", private=False):
            task = await feature_tasks.route(db, owner_id=owner, agent_id=employee.id, project="project",
                event_key=title, title=title, text=title)
            card = await db.get(WorkItem, task["task_id"])
            card.archived = True
            run = AgentJob(owner_id=owner, requester_agent_id=lead.id, requester_thread_id=thread.id,
                requester_turn_id=1, assignee_agent_id=employee.id, assignee_turn_id=23, task_id=card.id,
                kind="job", mode="work", brief=title, brief_hash=title, status="completed")
            db.add(run)
            await db.flush()
            db.add(AgentJobEvent(job_id=run.id, kind="completion_evidence", detail={
                "state":state, "turn_id":23, "evidence":[{"path":"PRIVATE_FILE_CONTENT"}]}))
            if private:
                db.add(AgentJobEvent(job_id=run.id, kind="feishu_assignment", detail={"contact":"private"}))
            await db.commit()
            return card.id

        verified = await job(chat, "核对导入文件")
        await job(chat, "导入文件只有口头汇报", state="reported")
        await job(chat, "私聊的导入文件", private=True)
        await job(other, "另一个会话里的导入文件")
        turn = await thread_store.start_turn(db, chat.id, "这次请核对导入文件的版本", origin="person")
        result = json.loads(await assignment_context.steward_context(db, owner, turn))
        assert result["verified_examples"] == [
            {"task":verified, "employee":employee.id, "topic":"核对导入文件", "verified":"files_verified"}]
        assert "PRIVATE_FILE_CONTENT" not in str(result)
        lead.recall_enabled = False
        await db.commit()
        assert not await assignment_context.steward_context(db, owner, turn)
