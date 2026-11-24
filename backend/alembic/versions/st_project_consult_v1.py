"""Bind durable team jobs to their feature card and record delivery requirements."""
from alembic import op
import sqlalchemy as sa

revision = "st_project_consult_v1"
down_revision = "opc_suggestions_v1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("steward_direct_turn",sa.Column("owner_id",sa.String(),primary_key=True),
        sa.Column("event_key",sa.String(),primary_key=True),sa.Column("thread_id",sa.BigInteger(),nullable=False),
        sa.Column("turn_id",sa.BigInteger(),nullable=False),sa.Column("input_sha256",sa.String(64),nullable=False))

    op.add_column("agent_job", sa.Column("task_id", sa.BigInteger(), nullable=True))
    op.add_column("agent_job", sa.Column("project", sa.String(), nullable=True))
    op.create_index("ix_agent_job_task", "agent_job", ["owner_id","task_id"])
    op.add_column("feature_scope", sa.Column("dependencies", sa.JSON(), nullable=True))
    op.add_column("feature_scope", sa.Column("deliverables", sa.JSON(), nullable=True))
    # Earlier Feishu hand-offs have an authoritative event-key binding.
    op.execute("""UPDATE agent_job SET task_id=(SELECT ws.task_id FROM feature_session ws
        WHERE ws.thread_id=agent_job.assignee_thread_id), project=(SELECT fs.project
        FROM feature_scope fs JOIN feature_session ws ON fs.task_id=ws.task_id
        WHERE ws.thread_id=agent_job.assignee_thread_id)
        WHERE kind='job' AND origin_ref IS NOT NULL AND EXISTS
        (SELECT 1 FROM feature_message m WHERE m.owner_id=agent_job.owner_id AND m.event_key=agent_job.origin_ref)""")


def downgrade():
    op.drop_table("steward_direct_turn")
    op.drop_column("feature_scope","deliverables")
    op.drop_column("feature_scope","dependencies")
    op.drop_index("ix_agent_job_task",table_name="agent_job")
    op.drop_column("agent_job","project")
    op.drop_column("agent_job","task_id")
