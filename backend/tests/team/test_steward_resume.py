from uuid import uuid4

from app.db.models import Thread
from app.services import agent_store, feature_tasks
from app.team import service
from tests.services.thread_harness import owner, sessions  # noqa: F401


async def test_reply_keeps_original_task_and_owner_words(sessions, owner, tmp_path):
    from types import SimpleNamespace
    from app.db.models import AgentJob, AgentJobEvent, WorkItem
    from app.services import attachments, thread_store
    from app.services.codex_adapter import CodexAdapter
    from app.services.thread_hub import ThreadHub
    from app.team.jobs import Jobs

    async with sessions() as db:
        lead = await agent_store.create_agent(db, owner_id=owner, name="大嘴", make_lead=True)
        staff = await agent_store.create_agent(db, owner_id=owner, name="Jensen", provider="codex")
        await service.change_member(db, owner, staff.id)
        successor = await agent_store.create_agent(db, owner_id=owner, name="二狗", provider="codex")
        await service.change_member(db, owner, successor.id)
        successor.role_path = str(tmp_path)
        staff.role_path = str(tmp_path)
        await db.commit()
        chat = await thread_store.create_thread(db, owner_id=owner, provider="codex", agent_id=lead.id,
                                               workspace_path=str(tmp_path))
    jobs = Jobs(SimpleNamespace(session=sessions, owner_id=owner, adapters={"codex": CodexAdapter()}, hub=ThreadHub()))

    async def assign(text, title, *, source=None, files=(), employee=None, retry=False, brief="精简过的摘要", project="P"):
        async with sessions() as db:
            turn = await thread_store.start_turn(db, chat.id, text, origin="person",
                attachments=[a.public() for a in files], **({"assignment_source": source} if source else {}))
        run = SimpleNamespace(agent_id=lead.id, thread_id=chat.id, turn_id=turn.id, owner_token=turn.owner_token,
            ctx=SimpleNamespace(approval_mode="ask", team=SimpleNamespace(team_id=lead.team_id, origin="person")))
        args = {"project":project, "task_title":title, "agent_id":(employee or staff).id,
                "brief":brief, "done_when":"跑完测试"}
        job = await jobs.enqueue(run, args, reply="resume")
        if retry:
            retried = await jobs.enqueue(run, {**args, "brief":"重试过程中重新生成的摘要"}, reply="resume")
            assert retried.id == job.id
        async with sessions() as db:
            await thread_store.finish_turn(db, turn.id, turn.owner_token, status="succeeded")
        return turn, job

    words = "让 Jensen 检查导入。" + "范围请保持原样。" * 65 + "修完后跑测试，附上失败样例。"
    file = attachments.save(chat.id, "样例.md", "text/markdown", b"original fixture")
    first, older = await assign(words, "导入", files=[file])
    calendar = attachments.save(chat.id, "日历.md", "text/markdown", b"owner calendar")
    later_turn, later = await assign("让 Jensen 更新日历。", "日历", files=[calendar], project="Q")
    async with sessions() as db:
        employee = await db.get(type(staff), staff.id)
        employee.name = "工程师小林"
        await db.commit()
    followup, resumed = await assign("继续，先测 Windows。", "勿使用重新概括的标题", source={"reply_to_turn_id": first.id},project="Q")
    assert resumed.task_id == older.task_id != later.task_id and resumed.project == "P"
    async with sessions() as db:
        from app.services.assignment_context import employee_input, steward_context
        requests, references = await employee_input(db, resumed)
        assert [r["text"] for r in requests] == [words, "继续，先测 Windows。"]
        assert [(r["thread_id"], r["id"]) for r in references] == [(chat.id, file.id)]
        assert resumed.assignee_agent_id == staff.id and resumed.brief == "精简过的摘要"
        assert (await db.get(WorkItem, older.task_id)).agent_id == staff.id
        candidates = await steward_context(db, owner, followup)
        assert "导入" in candidates and words not in candidates
        db.add(AgentJobEvent(job_id=older.id,kind="feishu_assignment",detail={"original":"PRIVATE_CHAT_WORDS"}))
        db.add(AgentJobEvent(job_id=older.id,kind="feishu_tool_started",detail={"tool":"feishu_publish_knowledge"}))
        for tool, stage, details in [
            ("feishu_edit_document", "feishu_tool_failed", {"category":"ReadTimeout"}),
            ("feishu_create_document", "feishu_tool_failed", {"category":"invalid_input"}),
            ("feishu_delete_document", "feishu_superseded", {"result":{"deleted":True}}),
        ]:
            db.add(AgentJobEvent(job_id=older.id, kind="feishu_tool_started", detail={"tool":tool}))
            db.add(AgentJobEvent(job_id=older.id, kind=stage, detail={"tool":tool, **details}))
        await db.commit()
    correction = "这单给错人了，交给二狗。已经做的别做第二遍。"
    _, changed = await assign(correction,"新标题",source={"reply_to_turn_id":first.id},employee=successor,retry=True,
                              brief="PRIVATE_CHAT_WORDS copied into the proposed handoff")
    async with sessions() as db:
        import pytest
        from app.services.assignment_context import handoff_progress, resolve_assignment_context, resolve_card
        assert changed.task_id == older.task_id and changed.assignment_epoch == 2
        assert "PRIVATE_CHAT_WORDS" not in changed.brief
        assert (await db.get(AgentJob, older.id)).status == (await db.get(AgentJob, resumed.id)).status == "cancelled"
        assert (await db.get(AgentJob, later.id)).status == "queued"
        new_messages, refs = await employee_input(db, changed)
        assert [r["text"] for r in new_messages] == [correction] and not refs
        assert await feature_tasks.private_handoff(db,changed.task_id)
        progress = await handoff_progress(db, changed)
        assert progress["remote_write_receipts"] == 1 and progress["remote_writes_to_reconcile"] == 2
        assert "PRIVATE_CHAT_WORDS" not in str(progress) and not progress["accessible_files"]
        stale = await resolve_assignment_context(db, owner, followup.id)
        with pytest.raises(ValueError,match="已改派"):
            await resolve_card(db, owner, stale, staff.id, {})
    _, open_handoff = await assign("改派给二狗，按原要求跟进。","日历",source={"reply_to_turn_id":later_turn.id},employee=successor)
    async with sessions() as db:
        messages, refs = await employee_input(db, open_handoff)
        assert [m["text"] for m in messages] == ["让 Jensen 更新日历。", "改派给二狗，按原要求跟进。"]
        assert [(r["thread_id"], r["id"]) for r in refs] == [(chat.id, calendar.id)]
        empty = await thread_store.create_thread(db,owner_id=owner,provider="codex",agent_id=lead.id,
                                                 workspace_path=str(tmp_path))
        stray = await thread_store.start_turn(db,empty.id,"请处理这个。",origin="person")
        other = await resolve_assignment_context(db, owner, stray.id)
        with pytest.raises(ValueError,match="不属于"):
            await resolve_card(db, owner, other, successor.id, {"project":"Q","task_id":open_handoff.task_id})
    separate_run = SimpleNamespace(agent_id=lead.id, thread_id=empty.id, turn_id=stray.id, owner_token=stray.owner_token,
        ctx=SimpleNamespace(approval_mode="ask", team=SimpleNamespace(team_id=lead.team_id, origin="person")))
    separate = await jobs.enqueue(separate_run, {"project":"Q","agent_id":successor.id,"task_title":"日历",
        "brief":"独立的日历工作","done_when":"跑完测试"}, reply="resume")
    assert separate.task_id != open_handoff.task_id
    async with sessions() as db:
        material = attachments.save(chat.id,"要求.md","text/markdown",b"original requirements")
        source = await thread_store.start_turn(db,chat.id,"这里是正式验收要求。",origin="person",attachments=[material.public()])
        await thread_store.finish_turn(db,source.id,source.owner_token,status="succeeded")
    _, quoted = await assign("让二狗处理这份材料。","材料核对",source={"reply_to_turn_id":source.id},employee=successor)
    async with sessions() as db:
        messages, refs = await employee_input(db, quoted)
        assert [m["text"] for m in messages] == ["这里是正式验收要求。", "让二狗处理这份材料。"]
        assert [(r["thread_id"], r["id"]) for r in refs] == [(chat.id, material.id)]
    _, numbered = await assign(f"请继续 #{open_handoff.task_id}，按原要求跟进。","旧日历",employee=successor,project="wrong")
    assert numbered.task_id == open_handoff.task_id and numbered.project == "Q"



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
