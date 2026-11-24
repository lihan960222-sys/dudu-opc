"""Exercise the project hierarchy with the real scheduler, tool bridge and a scripted model."""
import secrets
import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.models import Agent, AgentJob, AgentJobInbox, FeatureMessage, Thread, Turn
from app.services import agent_store, role_workspace, workspace_snapshots
from app.team import service, setup
from tests.services.test_codex_adapter import codex, crt  # noqa: F401
from tests.services.thread_harness import owner, sessions, wait_until  # noqa: F401


@pytest.fixture
def isolated_snapshots(monkeypatch):
    # Snapshot blob names are long. Keep this store short even with a deep --basetemp.
    with tempfile.TemporaryDirectory(prefix="opc-snap-") as folder:
        monkeypatch.setattr(workspace_snapshots, "store_dir_for", lambda path: Path(folder) / Path(path).name)
        yield


@pytest.mark.skipif(sys.platform != "win32", reason="Windows scoped Codex")
async def test_hire_dispatch_read_and_report(crt, sessions, owner, codex, tmp_path, monkeypatch, isolated_snapshots):
    monkeypatch.setattr(settings, "TEAM_ENABLED", True)
    monkeypatch.setattr(settings, "TEAM_LEAD_AUTO", True)
    monkeypatch.setattr(settings, "STEWARD_STRICT_OWNER", owner)
    async with sessions() as db:
        lead = await agent_store.create_agent(db, owner_id=owner, name="Lead", provider="codex", model="gpt-fake", make_lead=True)
        manager = await agent_store.create_agent(db, owner_id=owner, name="Jensen", provider="codex", model="gpt-fake")
        await service.change_member(db, owner, manager.id)
        lead.can_staff_team = True
        manager.role_path = str(tmp_path / "manager")
        Path(manager.role_path).mkdir()
        role_workspace.initialize(Path(manager.role_path), manager.model, "Inspect materials")
        await db.commit()

    evidence = "evidence-" + secrets.token_hex(8)
    initialize = setup._initialize_reviewed_role

    def seed_role(agent, cleanup):
        initialize(agent, cleanup)
        (Path(agent.role_path) / "acceptance.txt").write_text(evidence, encoding="utf8")

    monkeypatch.setattr(setup, "_initialize_reviewed_role", seed_role)
    codex.plan(
        {"type": "team", "tool": "delegate", "arguments": {
            "agent_ids": [manager.id], "project": "P", "brief": "Hire a reader to inspect the sample",
            "done_when": "Return the value found in acceptance.txt",
        }},
        {"type": "team", "tool": "propose_direct_report", "arguments": {
            "name": "Project reader", "title": "Reader", "instructions": "Read materials",
            "first_task": "Read acceptance.txt in role materials",
        }},
        {"type": "team", "tool": "review_direct_report", "review_proposal": True},
        {"type": "team", "tool": "delegate", "delegate_hired": True, "arguments": {
            "brief": "Read acceptance.txt", "done_when": "Return its content",
        }},
        {"type": "team", "server": "feature_files", "tool": "read_text",
         "arguments": {"scope": "role", "path": "acceptance.txt"}, "echo_tool": True},
        {"type": "relay_report"},
        {"type": "relay_report"},
    )
    chat = await crt.create_thread(provider="codex", agent_id=lead.id)
    original = await crt.send_turn(chat.id, "Have Jensen recruit a reader for this inspection and report the sample.")

    async def result_ready():
        async with sessions() as db:
            result = await db.scalar(select(Turn.result).where(Turn.thread_id == chat.id).order_by(Turn.seq.desc()).limit(1))
            return evidence in (result or "")

    await wait_until(result_ready, timeout=45)
    async with sessions() as db:
        reader = await db.scalar(select(Agent).where(Agent.owner_id == owner, Agent.name == "Project reader"))
        assert reader.manager_agent_id == manager.id and not reader.delegation_enabled
        root, parent, child = (await db.scalars(select(AgentJob).where(AgentJob.owner_id == owner).order_by(AgentJob.id))).all()
        child_turns = (await db.scalars(select(Turn).where(Turn.thread_id == child.assignee_thread_id))).all()
        returns = (await db.scalars(select(AgentJobInbox).where(AgentJobInbox.job_id == child.id))).all()
        assert all(receipt.holder_id == parent.id and receipt.delivered_at for receipt in returns) and returns
        assert root.requester_turn_id == original.id and parent.parent_id == root.id and child.parent_id == parent.id
        assert child.assignee_agent_id == reader.id and child.approval_mode == reader.approval_mode == "plan"
        assert child.status == parent.status == "completed" and child.project == parent.project == "P"
        assert (child.requester_agent_id, parent.requester_agent_id) == (manager.id, lead.id)
        assert len(child_turns) == 1 and evidence in child_turns[0].result
        final = await db.scalar(select(Turn).where(Turn.thread_id == chat.id).order_by(Turn.seq.desc()).limit(1))
        assert child.result == parent.result == final.result

        # Retransmission of a queued assignment still points to the first turn, even after internal wakes.
        from app.services import feature_tasks
        receipt = await db.get(FeatureMessage, (owner, parent.origin_ref))
        thread = await db.get(Thread, parent.assignee_thread_id)
        first = await db.scalar(select(Turn).where(Turn.thread_id == thread.id).order_by(Turn.seq).limit(1))
        assert parent.assignee_turn_id != first.id
        recovered = await feature_tasks.submission(db, thread, receipt.event_key, first.prompt_text, [])
        assert recovered.id == first.id

        reader.manager_agent_id = lead.id
        await db.flush()
        message = await crt.team_jobs._resume_text(db, parent, returns)
        assert evidence not in message and "different supervisor" in message
