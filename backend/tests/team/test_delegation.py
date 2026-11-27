"""Non-blocking delegation: hand off, end the turn, resume once with results."""
import asyncio
import json
import pytest
from sqlalchemy import select, func, update
from app.core.config import settings
from app.db.models import Agent, AgentJob, AgentJobInbox, Event, Turn
from app.services import agent_store, thread_store
from app.services.provider_adapter import TurnOutcome
from app.services.thread_hub import ThreadHub
from app.services.thread_runtime import ThreadRuntime
from app.team import service
from tests.services.thread_harness import sessions, owner, cleanup, wait_until  # noqa: F401
from tests.team.test_collaboration import Engine


async def test_reassign_fences_late_receipt(sessions, owner, tmp_path):
    from app.db.models import Approval, FeatureScope, WorkItem
    from app.services import feature_tasks
    # This case creates durable feature records that outlive the chat teardown
    # used by the general collaboration harness.
    owner += "-feature"
    async with sessions() as db:
        import hashlib
        lead = await agent_store.create_agent(db, owner_id=owner, name="Lead", make_lead=True)
        old = await agent_store.create_agent(db, owner_id=owner, name="First", provider="codex")
        new = await agent_store.create_agent(db, owner_id=owner, name="Second", provider="codex")
        for employee in (old,new):
            await service.change_member(db, owner, employee.id)
        old.role_path, new.role_path = str(tmp_path/"first"), str(tmp_path/"second")
        new.team_workspaces_readable = True
        await db.commit()
        intake = await feature_tasks.route(db, owner_id=owner, agent_id=old.id, project="P",
            title="Review import", text="Repair import and run tests", event_key="original")
        task_id = intake["task_id"]
        first, _ = await feature_tasks.session_for(db, owner, task_id, model="m", workspace_path=str(tmp_path))
        job_id = await feature_tasks.dispatch(db, owner_id=owner, steward_id=lead.id, task_id=task_id,
            event_key="original", thread_id=first.id)
        turn = await thread_store.start_turn(db, first.id, "repair", feature_event_key="original")
        (tmp_path/"first"/"任务"/str(task_id)).mkdir(parents=True)
        output = tmp_path/"first"/"result.md"
        output.write_bytes(b"original")
        digest = hashlib.sha256(b"original").hexdigest()
        await thread_store._add_event(db,first.id,"tool.completed",{"file_receipts":[{"operation":"fileChange",
            "scope":"role","root":str(tmp_path/"first"),"path":"result.md","sha256":digest}]},turn.id)
        await db.commit()
        ask = await thread_store.create_approval(db, thread_id=first.id, turn_id=turn.id,
            request_id="write", tool="feature_files.write_text", input={"path":"result","text":"old"})
        first_id, turn_id, token, ask_id, next_id = first.id, turn.id, turn.owner_token, ask.id, new.id
        subcard = await feature_tasks.route(db, owner_id=owner, agent_id=new.id, project="P",
            title="Verify old result", text="Read back the old pass", event_key="subtask")
        subthread, _ = await feature_tasks.session_for(db,owner,subcard["task_id"],model="m",workspace_path=str(tmp_path))
        subjob_id = await feature_tasks.dispatch(db,owner_id=owner,steward_id=lead.id,task_id=subcard["task_id"],
                                                event_key="subtask",thread_id=subthread.id)
        subjob = await db.get(AgentJob,subjob_id)
        subjob.parent_id, subjob.depth = job_id, 2
        await db.commit()
        subturn = await thread_store.start_turn(db,subthread.id,"verify",feature_event_key="subtask")
        subthread_id, subturn_id, subtoken = subthread.id, subturn.id, subturn.owner_token
        before = await feature_tasks.reassign(db, task_id, 1, new.id, "correction", owner_id=owner)
        assert before["task_id"] == task_id and before["assignment_epoch"] == 2
        await db.commit()
        assert await feature_tasks.reassign(db, task_id, 1, new.id, "correction", owner_id=owner) == before
        with pytest.raises(ValueError):
            await feature_tasks.reassign(db, task_id, 1, old.id, "different-correction", owner_id=owner)
    async with sessions() as db:
        assert not await thread_store.decide_approval(db, ask_id, "allowed")
        with pytest.raises(thread_store.TurnNotRunning):
            await thread_store.create_approval(db,thread_id=subthread_id,turn_id=subturn_id,request_id="late",
                                               tool="feature_files.write_text",input={"path":"late","text":"old"})
        await thread_store.finish_turn(db,subturn_id,subtoken,status="cancelled")
        await thread_store.finish_turn(db, turn_id, token, status="succeeded", result="private old content")
        from types import SimpleNamespace
        from app.db.models import AgentJobEvent
        from app.team.jobs import Jobs
        await Jobs(SimpleNamespace(session=sessions,owner_id=owner)).on_turn_end(turn_id)
        late = select(func.count()).select_from(AgentJobEvent).where(AgentJobEvent.job_id==job_id,
                                                                    AgentJobEvent.kind=="late_receipt")
        assert await db.scalar(late) == 1
        assert not await feature_tasks.report(db,owner,"original",first_id,turn_id)
        await db.commit()
        assert await db.scalar(late) == 1
    async with sessions() as db:
        card = await db.get(WorkItem, task_id)
        old_job = await db.get(AgentJob, job_id)
        assert card.agent_id == next_id and (await db.get(FeatureScope, task_id)).assignment_epoch == 2
        assert old_job.assignment_epoch == 1 and old_job.status == "cancelled"
        assert (await db.get(Approval, ask_id)).status == "cancelled"
        assert not await db.scalar(select(AgentJobInbox.id).where(AgentJobInbox.job_id==old_job.id))
        successor, _ = await feature_tasks.session_for(db, owner, task_id, model="m", workspace_path=str(tmp_path))
        assert successor.id != first_id and successor.agent_id == next_id
        from types import SimpleNamespace
        from app.services.assignment_context import handoff_progress
        assigned = SimpleNamespace(owner_id=owner, task_id=task_id, assignment_epoch=2, assignee_agent_id=next_id)
        progress = await handoff_progress(db,assigned)
        assert progress["file_receipts"] == 1
        assert progress["accessible_files"][0]["status"] == "verified"
        output.write_bytes(b"manual change")
        assert (await handoff_progress(db,assigned))["accessible_files"][0]["status"] == "changed"
        assert output.read_bytes() == b"manual change"



class FakeEngine(Engine):
    nested = None  # set by a test: records what a blocking job's own delegate call returns

    async def run_turn(self, ctx, sink, gate):
        if 'NESTED' in ctx.prompt and self.nested is not None:
            outcome = await self.nested(ctx)
            if isinstance(outcome, TurnOutcome):
                return outcome
        if ctx.thread_id not in self.handlers:
            if 'FAIL' in ctx.prompt:
                self.contexts.append(ctx)
                return TurnOutcome('failed', error='boom', error_code='provider_error')
            if 'ASK' in ctx.prompt and 'Clarification from the assigning steward' not in ctx.prompt:
                self.contexts.append(ctx)
                await sink.emit('assistant.message', {'text': 'partial'})
                question = '把结果交给哪位负责人？' if '把结果交给负责人' in ctx.prompt else 'which environment?'
                return TurnOutcome('succeeded', result=f'partial work\nNEEDS INPUT: {question}')
        return await super().run_turn(ctx, sink, gate)


@pytest.fixture
async def team(sessions, owner, monkeypatch, tmp_path):
    from app.services import local_claude_runner
    monkeypatch.setattr(local_claude_runner, 'workspace_dir', lambda: tmp_path)
    monkeypatch.setattr(settings, 'TEAM_ENABLED', True)
    engine = FakeEngine()
    rt = ThreadRuntime(hub=ThreadHub(), adapters={'claude': engine}, owner_id=owner, _session_factory=sessions)
    async with sessions() as db:
        lead = await agent_store.create_agent(db, owner_id=owner, name='Lead')
        one = await agent_store.create_agent(db, owner_id=owner, name='Reviewer', approval_mode='ask')
        two = await agent_store.create_agent(db, owner_id=owner, name='Drafter', approval_mode='ask')
        await service.set_lead(db, owner, lead.id)
        await service.change_member(db, owner, one.id)
        await service.change_member(db, owner, two.id)
    yield rt, engine, lead, one, two
    # Let background work finish before the database is cleaned.
    async with sessions() as db:
        live = list((await db.execute(select(AgentJob.id).where(AgentJob.owner_id == owner, AgentJob.status.in_(('queued', 'running', 'needs_input', 'waiting', 'resume'))))).scalars())
    for identifier in live:
        await rt.team_jobs.cancel(identifier)
    for _ in range(200):
        if not rt._runs:
            break
        await asyncio.sleep(.05)
    # A run removes itself before scheduling its notice hook. Those hooks
    # still use database sessions, so empty _runs alone is not a teardown
    # boundary. Finish them before cleanup deletes rows or disposes the engine.
    await wait_until(lambda: not rt._runs and not rt.notices.tasks)
    # Prevent new passes and drain the existing pass rather than cancelling
    # it in the middle of a SQLite write transaction.
    rt._closing = True
    if rt.team_jobs._kick_task is not None:
        await asyncio.gather(rt.team_jobs._kick_task, return_exceptions=True)
    await rt.shutdown()
    await asyncio.gather(*rt.rooms.tasks.values(), return_exceptions=True)
    await wait_until(lambda: not rt._runs and not rt.notices.tasks)


def lead_script(rt, engine, thread, first):
    """The lead's first turn runs `first(run)`; later turns just answer."""
    calls = []
    async def handler(ctx, sink, gate):
        calls.append(ctx)
        if len(calls) == 1:
            await first(rt._runs[ctx.turn_id])
            return TurnOutcome('succeeded', result='Handed out')
        await sink.emit('assistant.message', {'text': 'Final answer'})
        return TurnOutcome('succeeded', result='Final answer')
    engine.handlers[thread.id] = handler
    return calls


async def turns_of(sessions, thread_id):
    async with sessions() as db:
        return list((await db.execute(select(Turn).where(Turn.thread_id == thread_id).order_by(Turn.id))).scalars())


async def root_of(sessions, thread_id):
    async with sessions() as db:
        return await db.scalar(select(AgentJob).where(AgentJob.requester_thread_id == thread_id, AgentJob.kind == 'root'))


async def test_delegate_returns_at_once_and_lead_is_resumed_exactly_once(team, sessions):
    rt, engine, lead, one, two = team
    thread = await rt.create_thread(agent_id=lead.id)
    receipts = {}
    async def first(run):
        receipts.update(await rt.team_jobs.delegate(run, {'agent_ids': [one.id], 'brief': 'Review the patch', 'done_when': 'A verdict'}))
    calls = lead_script(rt, engine, thread, first)
    await rt.send_turn(thread.id, 'please review')
    async def done():
        root = await root_of(sessions, thread.id)
        return root and root.status == 'completed'
    await wait_until(done)
    assert receipts['receipts'][0]['status'] == 'assigned' and 'not completed' in receipts['note']
    turns = await turns_of(sessions, thread.id)
    assert len(turns) == 2 and len(calls) == 2
    assert 'Harness message' in turns[1].prompt_text and 'Specialist result' in turns[1].prompt_text
    assert 'Reviewer' in calls[0].prompt and 'delegate' in calls[0].prompt  # roster and lead paragraph
    async with sessions() as db:
        root = await root_of(sessions, thread.id)
        child = await db.scalar(select(AgentJob).where(AgentJob.parent_id == root.id))
        assert (child.status, child.outcome, child.reply, child.depth) == ('completed', 'done', 'resume', 1)
        items = list((await db.execute(select(AgentJobInbox).where(AgentJobInbox.holder_id == root.id))).scalars())
        assert len(items) == 1 and items[0].delivered_at is not None
        assert root.result == 'Final answer' and root.executions == 1


async def test_two_teammates_settle_into_one_resume(team, sessions):
    rt, engine, lead, one, two = team
    thread = await rt.create_thread(agent_id=lead.id)
    async def first(run):
        out = await rt.team_jobs.delegate(run, {'agent_ids': [one.id, two.id], 'brief': 'Split the work'})
        assert [r['status'] for r in out['receipts']] == ['assigned', 'assigned']
    lead_script(rt, engine, thread, first)
    await rt.send_turn(thread.id, 'go')
    async def done():
        root = await root_of(sessions, thread.id)
        return root and root.status == 'completed'
    await wait_until(done)
    turns = await turns_of(sessions, thread.id)
    assert len(turns) == 2
    body = json.loads(turns[1].prompt_text.split('\n', 1)[1])
    assert len(body['reports']) == 2 and {r['teammate'] for r in body['reports']} == {'Reviewer', 'Drafter'}


async def test_failed_job_is_reported_and_asking_again_runs_again(team, sessions):
    rt, engine, lead, one, two = team
    thread = await rt.create_thread(agent_id=lead.id)
    ids = []
    async def first(run):
        out = await rt.team_jobs.delegate(run, {'agent_ids': [one.id], 'brief': 'FAIL this one'})
        ids.append(out['receipts'][0]['job_id'])
    lead_script(rt, engine, thread, first)
    await rt.send_turn(thread.id, 'go')
    async def done():
        root = await root_of(sessions, thread.id)
        return root and root.status == 'completed'
    await wait_until(done)
    turns = await turns_of(sessions, thread.id)
    assert '"status": "failed"' in turns[1].prompt_text and 'provider_error' in turns[1].prompt_text
    # The failed job is not returned as if it were an answer.
    thread2 = await rt.create_thread(agent_id=lead.id)
    again = []
    async def second(run):
        again.append(await rt.team_jobs.delegate(run, {'agent_ids': [one.id], 'brief': 'FAIL this one'}))
        again.append(await rt.team_jobs.delegate(run, {'agent_ids': [one.id], 'brief': 'FAIL this one'}))
    lead_script(rt, engine, thread2, second)
    await rt.send_turn(thread2.id, 'go again')
    await wait_until(lambda: len(again) == 2)
    assert again[0]['receipts'][0]['job_id'] == again[1]['receipts'][0]['job_id'] != ids[0]


async def test_question_parks_then_the_persons_answer_runs_the_job_again(team, sessions):
    rt, engine, lead, one, two = team
    thread = await rt.create_thread(agent_id=lead.id)
    async def first(run):
        await rt.team_jobs.delegate(run, {'agent_ids': [one.id], 'brief': 'ASK about deploy'})
    calls = lead_script(rt, engine, thread, first)
    await rt.send_turn(thread.id, 'deploy it')
    async def parked():
        async with sessions() as db:
            return await db.scalar(select(AgentJob).where(AgentJob.status == 'needs_input', AgentJob.question.is_not(None), AgentJob.requester_thread_id == thread.id))
    job = await wait_until(parked)
    assert job.question == 'which environment?'
    await wait_until(lambda: len(calls) == 2)  # the lead is resumed with the question, once
    turns = await turns_of(sessions, thread.id)
    assert 'which environment?' in turns[1].prompt_text and 'questions' in turns[1].prompt_text
    root = await root_of(sessions, thread.id)
    async def waiting():
        async with sessions() as db:
            return (await db.get(AgentJob, root.id)).status == 'waiting'
    await wait_until(waiting)
    # The person answers in the teammate's own conversation.
    await rt.send_turn(job.assignee_thread_id, 'staging')
    async def finished():
        async with sessions() as db:
            return (await db.get(AgentJob, root.id)).status == 'completed'
    await wait_until(finished)
    assert len(await turns_of(sessions, thread.id)) == 3
    async with sessions() as db:
        done = await db.get(AgentJob, job.id)
        assert done.status == 'completed' and done.answer == 'staging'



async def test_lead_clarifies_question_in_place(team, sessions):
    rt, engine, lead, employee, _ = team
    thread = await rt.create_thread(agent_id=lead.id)
    receipts = []
    async def handler(ctx, sink, gate):
        run = rt._runs[ctx.turn_id]
        if not receipts:
            assigned = await rt.team_jobs.delegate(run, {'agent_ids':[employee.id], 'brief':'ASK about scope'})
            receipts.append(assigned['receipts'][0]['job_id'])
        elif 'which environment?' in ctx.prompt:
            question = json.loads(ctx.prompt.split('\n')[-1])['questions'][0]
            args = {'job_id':receipts[0], 'question_turn_id':question['question_turn_id'], 'answer':'staging'}
            receipts.append(await rt.team_jobs.answer_question(run, args))
            with pytest.raises(service.TeamError, match='no longer open'):
                await rt.team_jobs.answer_question(run, args)
        return TurnOutcome('succeeded', result='Noted')
    engine.handlers[thread.id] = handler
    await rt.send_turn(thread.id, 'review staging')
    async def completed():
        root = await root_of(sessions, thread.id)
        return root and root.status == 'completed'
    await wait_until(completed)
    async with sessions() as db:
        job = await db.get(AgentJob, receipts[0])
        assert job.status == 'completed' and job.answer == 'staging' and job.approval_mode == 'ask'
        assert len(await turns_of(sessions, job.assignee_thread_id)) == 2
    assert receipts[1]['status'] == 'queued'


async def test_expired_business_question_cannot_restart_employee(team, sessions, monkeypatch):
    rt, engine, lead, employee, _ = team
    chat = await rt.create_thread(agent_id=lead.id)
    receipts = []

    async def handler(ctx, sink, gate):
        run = rt._runs[ctx.turn_id]
        if not receipts:
            result = await rt.team_jobs.delegate(run, {'agent_ids':[employee.id], 'brief':'ASK about scope'})
            receipts.append(result['receipts'][0]['job_id'])
        elif 'which environment?' in ctx.prompt:
            question = json.loads(ctx.prompt.split('\n')[-1])['questions'][0]
            monkeypatch.setattr(settings, "TEAM_QUESTION_WAIT_SECONDS", 0)
            await rt.team_jobs.expire()
            with pytest.raises(service.TeamError, match="no longer open"):
                await rt.team_jobs.answer_question(run, {'job_id':receipts[0],
                    'question_turn_id':question['question_turn_id'],'answer':'staging'})
            receipts.append('refused')
        return TurnOutcome('succeeded', result='Noted')

    engine.handlers[chat.id] = handler
    await rt.send_turn(chat.id, 'review staging')
    await wait_until(lambda: 'refused' in receipts)
    async with sessions() as db:
        job = await db.get(AgentJob, receipts[0])
        assert job.outcome == 'expired' and job.answer is None
        assert len(await turns_of(sessions, job.assignee_thread_id)) == 1

async def test_stop_while_waiting_cancels_and_never_resumes(team, sessions):
    rt, engine, lead, one, two = team
    thread = await rt.create_thread(agent_id=lead.id)
    async def first(run):
        await rt.team_jobs.delegate(run, {'agent_ids': [one.id], 'brief': 'BLOCK forever'})
    lead_script(rt, engine, thread, first)
    await rt.send_turn(thread.id, 'go')
    async def waiting():
        root = await root_of(sessions, thread.id)
        return root and root.status == 'waiting'
    await wait_until(waiting)
    await rt.stop_turn(thread.id)
    async with sessions() as db:
        root = await root_of(sessions, thread.id)
        child = await db.scalar(select(AgentJob).where(AgentJob.parent_id == root.id))
        assert root.status == 'cancelled' and child.status == 'cancelled'
    await wait_until(lambda: not rt._runs)
    await rt.team_jobs.tick()
    assert len(await turns_of(sessions, thread.id)) == 1


async def test_budget_and_parallel_limits(team, sessions, monkeypatch):
    rt, engine, lead, one, two = team
    monkeypatch.setattr(settings, 'TEAM_MAX_PARALLEL', 1)
    thread = await rt.create_thread(agent_id=lead.id)
    async def first(run):
        await rt.team_jobs.delegate(run, {'agent_ids': [one.id, two.id], 'brief': 'Two at once'})
    lead_script(rt, engine, thread, first)
    await rt.send_turn(thread.id, 'go')
    async def done():
        root = await root_of(sessions, thread.id)
        return root and root.status == 'completed'
    await wait_until(done)
    assert len(await turns_of(sessions, thread.id)) == 2  # both results, one resume
    monkeypatch.setattr(settings, 'TEAM_MAX_JOBS_PER_ROOT', 1)
    thread2 = await rt.create_thread(agent_id=lead.id)
    out = {}
    async def second(run):
        out.update(await rt.team_jobs.delegate(run, {'agent_ids': [one.id, two.id], 'brief': 'Over budget'}))
    lead_script(rt, engine, thread2, second)
    await rt.send_turn(thread2.id, 'again')
    await wait_until(lambda: out)
    assert [r.get('code') for r in out['receipts']] == [None, 'root_budget']


async def test_resume_that_cannot_start_is_retried_not_lost(team, sessions, monkeypatch):
    rt, engine, lead, one, two = team
    thread = await rt.create_thread(agent_id=lead.id)
    async def first(run):
        await rt.team_jobs.delegate(run, {'agent_ids': [one.id], 'brief': 'Do it'})
    lead_script(rt, engine, thread, first)
    real = rt.send_turn
    state = {'failed': 0}
    async def flaky(thread_id, text, **kw):
        if thread_id == thread.id and text.startswith('Harness message') and state['failed'] < 2:
            state['failed'] += 1
            raise thread_store.ThreadBusy(thread_id)
        return await real(thread_id, text, **kw)
    monkeypatch.setattr(rt, 'send_turn', flaky)
    await rt.send_turn(thread.id, 'go')
    async def done():
        await rt.team_jobs.tick()  # no scheduler runs here: a refused resume is retried by the next pass
        root = await root_of(sessions, thread.id)
        return root and root.status == 'completed'
    await wait_until(done)
    assert state['failed'] == 2
    turns = await turns_of(sessions, thread.id)
    assert len(turns) == 2
    async with sessions() as db:
        root = await root_of(sessions, thread.id)
        assert (await db.scalar(select(func.count()).select_from(AgentJobInbox).where(AgentJobInbox.holder_id == root.id, AgentJobInbox.delivered_at.is_(None)))) == 0


async def test_interrupted_child_is_failed_and_the_lead_is_told(team, sessions):
    rt, engine, lead, one, two = team
    thread = await rt.create_thread(agent_id=lead.id)
    async def first(run):
        await rt.team_jobs.delegate(run, {'agent_ids': [one.id], 'brief': 'BLOCK a while'})
    lead_script(rt, engine, thread, first)
    await rt.send_turn(thread.id, 'go')
    async def running():
        async with sessions() as db:
            return await db.scalar(select(AgentJob).where(AgentJob.requester_thread_id == thread.id, AgentJob.kind == 'job', AgentJob.current_turn_id.is_not(None)))
    child = await wait_until(running)
    # The process "died": the turn is interrupted after work began, then it is swept.
    async with sessions() as db:
        await thread_store._add_event(db, child.assignee_thread_id, 'assistant.message', {'text': 'partway'}, child.current_turn_id)
        await thread_store._interrupt(db, child.current_turn_id, reason='lease_expired')
    active = rt._runs.get(child.current_turn_id)
    if active:
        active.ctx.abort()
    async def settled():
        async with sessions() as db:
            return (await db.get(AgentJob, child.id)).status == 'failed'
    for _ in range(40):
        await rt.team_jobs.recover()
        if await settled():
            break
        await asyncio.sleep(.1)
    async with sessions() as db:
        job = await db.get(AgentJob, child.id)
        assert (job.status, job.outcome) == ('failed', 'interrupted')
    async def done():
        root = await root_of(sessions, thread.id)
        return root and root.status == 'completed'
    await wait_until(done)
    assert 'interrupted' in (await turns_of(sessions, thread.id))[1].prompt_text


async def test_child_interrupted_before_the_engine_took_it_is_requeued_once(team, sessions):
    rt, engine, lead, one, two = team
    thread = await rt.create_thread(agent_id=lead.id)
    ids = {}
    async def first(run):
        out = await rt.team_jobs.enqueue(run, {'agent_id': one.id, 'brief': 'Never started'}, reply='resume')
        ids['job'] = out.id
    lead_script(rt, engine, thread, first)
    # Hold the child: admit nothing until we have staged the interruption.
    original = rt.team_jobs.dispatch_queued
    async def hold():
        return None
    rt.team_jobs.dispatch_queued = hold
    await rt.send_turn(thread.id, 'go')
    await wait_until(lambda: 'job' in ids)
    await wait_until(lambda: not rt._runs)
    async with sessions() as db:
        row = await db.get(AgentJob, ids['job'])
        turn = await thread_store.start_turn(db, (await rt.create_thread(agent_id=one.id, kind='job', job_id=row.id)).id, 'x', owner_instance='gone')
        await db.execute(update(AgentJob).where(AgentJob.id == row.id).values(status='running', current_turn_id=turn.id, assignee_turn_id=turn.id, assignee_thread_id=turn.thread_id))
        await db.commit()
        await thread_store._interrupt(db, turn.id, reason='lease_expired')
    rt.team_jobs.dispatch_queued = original
    await rt.team_jobs.reconcile()
    async with sessions() as db:
        row = await db.get(AgentJob, ids['job'])
        assert (row.status, row.restarts, row.restart) == ('queued', 1, 'rerun')
    await rt.team_jobs.tick()
    async def finished():
        async with sessions() as db:
            return (await db.get(AgentJob, ids['job'])).status == 'completed'
    await wait_until(finished)


async def test_job_status_refuses_this_turn_and_blocking_jobs_cannot_delegate(team, sessions):
    rt, engine, lead, one, two = team
    thread = await rt.create_thread(agent_id=lead.id)
    seen = {}
    async def nested(ctx):
        out = await rt.team_jobs.delegate(rt._runs[ctx.turn_id], {'agent_ids': [two.id], 'brief': 'deeper'})
        seen['nested'] = {'code': out.get('code')}
    engine.nested = nested
    async def first(run):
        out = await rt.team_jobs.delegate(run, {'agent_ids': [one.id], 'brief': 'Do it'})
        job_id = out['receipts'][0]['job_id']
        try:
            await rt.team_jobs.job_status(run, {'job_id': job_id})
        except service.TeamError as exc:
            seen['code'] = exc.code
        seen['listing'] = await rt.team_jobs.job_status(run, {})
    lead_script(rt, engine, thread, first)
    await rt.send_turn(thread.id, 'go')
    await wait_until(lambda: 'listing' in seen)
    assert seen['code'] == 'same_turn' and seen['listing']['jobs'] == []

    thread2 = await rt.create_thread(agent_id=lead.id)
    async def ask_blocking(ctx, sink, gate):
        seen['ask'] = await rt.team_jobs.ask(rt._runs[ctx.turn_id], {'agent_id': one.id, 'brief': 'NESTED please'})
        return TurnOutcome('succeeded', result='ok')
    engine.handlers[thread2.id] = ask_blocking
    await rt.send_turn(thread2.id, 'go')
    await wait_until(lambda: 'ask' in seen)
    assert seen['ask']['status'] == 'completed' and seen['nested']['code'] == 'use_ask'


async def test_person_message_gets_a_note_about_outstanding_work(team, sessions):
    rt, engine, lead, one, two = team
    thread = await rt.create_thread(agent_id=lead.id)
    async def first(run):
        await rt.team_jobs.delegate(run, {'agent_ids': [one.id], 'brief': 'BLOCK on it'})
    lead_script(rt, engine, thread, first)
    await rt.send_turn(thread.id, 'go')
    async def waiting():
        root = await root_of(sessions, thread.id)
        return root and root.status == 'waiting'
    await wait_until(waiting)
    note = await rt.team_jobs.outstanding_note(thread.id)
    assert 'Reviewer' in note and 'Do not assign it again' in note
    await rt.team_jobs.cancel((await root_of(sessions, thread.id)).id)

async def test_owner_clarifies_external_delivery_only_in_steward_chat(team, sessions):
    rt, engine, lead, employee, _ = team
    chat = await rt.create_thread(agent_id=lead.id)
    jobs, asked = [], []
    owner_answer = '不用外发，待领取'
    async def lead_turn(ctx, sink, gate):
        run = rt._runs[ctx.turn_id]
        if not jobs:
            assigned = await rt.team_jobs.delegate(run, {'agent_ids':[employee.id], 'brief':'ASK 把结果交给负责人'})
            jobs.append(assigned['receipts'][0]['job_id'])
        elif owner_answer in ctx.prompt and 'Harness message' not in ctx.prompt:
            row = (await rt.team_jobs.job_status(run, {'job_id':jobs[0]}))['jobs'][0]
            await rt.team_jobs.answer_question(run, {'job_id':jobs[0],'question_turn_id':row['question_turn_id'],'answer':owner_answer})
        elif '把结果交给哪位负责人' in ctx.prompt:
            asked.append(chat.id)
            return TurnOutcome('succeeded', result='负责人是谁，还是把结果留在项目里待领取？')
        return TurnOutcome('succeeded', result='已记录处理状态。')
    engine.handlers[chat.id] = lead_turn
    await rt.send_turn(chat.id, '把结果交给负责人')
    async def waiting():
        root = await root_of(sessions, chat.id)
        return asked and root and root.status=='waiting' and not rt._runs
    await wait_until(waiting)
    await rt.send_turn(chat.id, owner_answer)
    async def finished():
        root = await root_of(sessions, chat.id)
        return root and root.status=='completed'
    await wait_until(finished)
    async with sessions() as db:
        rows = list(await db.scalars(select(AgentJob).where(AgentJob.kind=='job',AgentJob.requester_thread_id==chat.id)))
        assert len(rows)==1 and rows[0].answer==owner_answer and rows[0].status=='completed'
        employee_turns=await turns_of(sessions,rows[0].assignee_thread_id)
        assert len(employee_turns)==2 and owner_answer.replace('，', ',') in employee_turns[-1].prompt_text
        assert asked == [chat.id]


@pytest.mark.parametrize("case",["complete","no_scope","blocked_after_write","engine_failed_after_write","readback_failed","deleted"])
async def test_steward_receives_verified_document_or_actual_scope_error(team,sessions,case):
    from types import SimpleNamespace
    from app.team.feishu_tools_bridge import record, record_delivery
    rt,engine,boss,worker,_=team
    thread=await rt.create_thread(agent_id=boss.id)
    tool="feishu_delete_document" if case=="deleted" else "feishu_publish_knowledge"
    repair="缺少 docx:document:readonly，请重新授权文档读写。"
    async def employee_runs(ctx):
        # The stubbed provider reports through the same transactional journal as the real bridge.
        async def assigned():
            async with sessions() as db:
                return await db.scalar(select(AgentJob.id).where(AgentJob.assignee_thread_id==ctx.thread_id,
                    AgentJob.assignee_turn_id==ctx.turn_id, AgentJob.kind=="job"))
        job_id = await wait_until(assigned)
        run = SimpleNamespace(agent_id=worker.id,turn_id=ctx.turn_id)
        action = "delete_document" if case=="deleted" else "restructure"
        await record(rt,job_id,run,tool,"feishu_assignment",action=action)
        if case!="no_scope":
            receipt={"deleted":True} if case=="deleted" else {"url":"https://example.feishu.cn/docx/a","messages_read":3}
            await record_delivery(rt,job_id,run,tool,tool,action,f"job-{job_id}",receipt)
            if case=="readback_failed":
                await record(rt,job_id,run,"feishu_read_document","feishu_tool_failed",detail=repair)
        else:
            await record(rt,job_id,run,tool,"feishu_tool_failed",detail=repair)
        if case=="engine_failed_after_write":
            return TurnOutcome("failed",error="engine failed",error_code="provider_error")
        return TurnOutcome("succeeded",result="BLOCKED: 尚未核对来源。" if case=="blocked_after_write" else "员工交付说明。")
    engine.nested=employee_runs
    async def assign(run):
        await rt.team_jobs.delegate(run,{"agent_ids":[worker.id],"brief":"NESTED","done_when":"doc link"})
    calls=lead_script(rt,engine,thread,assign)
    await rt.send_turn(thread.id,"update the document")
    async def done():
        root=await root_of(sessions,thread.id)
        return root if root and root.status in ("completed","failed") else None
    root=await wait_until(done)
    assert len(calls)==2
    report=(await turns_of(sessions,thread.id))[-1].prompt_text
    entry=json.loads(report.split("\n",1)[1])["reports"][0]
    if case in ("complete","deleted"):
        assert entry["verified_document"]["operation"]==tool
    elif case in ("blocked_after_write","engine_failed_after_write"):
        assert entry["observed_tool_receipt"]["operation"]==tool and "verified_document" not in entry
    else:
        assert "verified_document" not in entry
    if case in ("complete","deleted"):
        assert root.status=="completed" and entry["status"]=="completed" and "员工交付说明" in entry["result"]
        if case=="deleted":
            assert "url" not in entry["verified_document"]
    elif case=="engine_failed_after_write":
        assert entry["status"]=="failed" and entry["outcome"]=="provider_error"
    elif case=="blocked_after_write":
        assert entry["status"]=="failed" and "尚未核对来源" in entry["detail"]
    else:
        assert root.status=="failed" and "docx:document:readonly" in report and "missing_delivery" in report
