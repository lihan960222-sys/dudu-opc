"""The owner's steward DM can continue a card it assigned outside the gateway's default project."""
import json

import pytest

from app.db.models import AgentJob, FeishuLeadChat
from app.feishu import lead_chat
from app.services import agent_store, assignment_context, feature_tasks, thread_store
from app.team.service import TeamError
from tests.services.thread_harness import owner, sessions  # noqa: F401


async def test_private_steward_continues_its_own_numbered_or_quoted_card_across_projects(sessions, owner, tmp_path):
    async with sessions() as db:
        lead = await agent_store.create_agent(db, owner_id=owner, name='Steward', make_lead=True)
        worker = await agent_store.create_agent(db, owner_id=owner, name='Editor')

        async def chat(channel, selection):
            thread = await thread_store.create_thread(db, owner_id=owner, agent_id=lead.id,
                provider='codex', workspace_path=str(tmp_path))
            db.add(FeishuLeadChat(thread_id=thread.id, channel=channel, selection=selection,
                                  project='default project', isolated=True))
            await db.commit()
            return thread

        original = await chat('owner DM', 'model A')
        current = await chat('owner DM', 'model B')
        unrelated = await chat('other binding', 'model A')

        async def previous(thread, label):
            request = await thread_store.start_turn(db, thread.id, 'Please prepare these minutes.', origin='person')
            result = await feature_tasks.route(db, owner_id=owner, agent_id=worker.id,
                project='meeting notes', event_key=label, title=label, text='authorized work')
            db.add(AgentJob(owner_id=owner, requester_agent_id=lead.id, requester_thread_id=thread.id,
                           requester_turn_id=request.id, assignee_agent_id=worker.id, task_id=result['task_id'],
                           project='meeting notes', origin_kind='person', kind='job', mode='work',
                           brief=label, brief_hash=label, status='completed'))
            await db.commit()
            await thread_store.finish_turn(db, request.id, request.owner_token, status='succeeded')
            return result['task_id'], request.id

        expected, old_turn = await previous(original, 'original task')
        foreign_card, _ = await previous(unrelated, 'other task')

        async def ask(prompt, source=None):
            turn = await thread_store.start_turn(db, current.id, prompt, origin='person',
                **({'assignment_source': source} if source else {}))
            await thread_store.finish_turn(db, turn.id, turn.owner_token, status='succeeded')
            return turn

        for message, source in ((f'续办工单 #{expected}，让 Editor 查回原件', None),
                                ('把原来的继续整理', {'reply_to_turn_id': old_turn}),
                                ('继续整理', None)):
            turn = await ask(message, source)
            ctx = await assignment_context.resolve_assignment_context(db, owner, turn.id)
            card, scope = await assignment_context.resolve_card(db, owner, ctx, worker.id,
                {'assignment_intent': 'continue', 'project': 'model-proposed project'})
            assert (card.id, scope.project) == (expected, 'meeting notes')
            catalog = json.loads(await assignment_context.steward_context(db, owner, turn))
            assert [r['id'] for r in catalog['tasks']] == [expected]
            assert f'#{expected} ' in await assignment_context.status_text(db, owner, ctx)

        turn = await ask(f'续办工单 #{foreign_card}')
        with pytest.raises(TeamError, match='不属于') as denied:
            await assignment_context.resolve_assignment_context(db, owner, turn.id)
        assert denied.value.code == 'assignment_reference_scope'
        facts = await lead_chat.project_context(db, owner, 'default project', current.id)
        assert json.loads(facts.partition('：')[2]) == [[expected, worker.id]]
