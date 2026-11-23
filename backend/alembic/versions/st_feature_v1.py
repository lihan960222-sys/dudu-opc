from alembic import op
import sqlalchemy as sa
from app.db.types import JSONValue

revision = "st_feature_v1"
down_revision = "72b13fd94ce0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("feature_scope",
        sa.Column("task_id", sa.BigInteger(), sa.ForeignKey("work_item.id"), primary_key=True),
        sa.Column("project", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("acceptance", sa.Text(), nullable=False),
        sa.Column("aliases", JSONValue, nullable=False),
        sa.Column("checkpoint", sa.Text(), nullable=False),
        sa.Column("related_id", sa.BigInteger(), sa.ForeignKey("work_item.id")))
    op.create_table("feature_message",
        sa.Column("owner_id", sa.String(), primary_key=True),
        sa.Column("event_key", sa.String(), primary_key=True),
        sa.Column("agent_id", sa.BigInteger(), nullable=False),
        sa.Column("project", sa.String(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("result", JSONValue, nullable=False))
    op.create_table("feature_session",
        sa.Column("thread_id", sa.BigInteger(), sa.ForeignKey("thread.id"), primary_key=True),
        sa.Column("task_id", sa.BigInteger(), sa.ForeignKey("work_item.id"), nullable=False),
        sa.Column("previous_id", sa.BigInteger(), sa.ForeignKey("thread.id")))
    op.create_index("ix_feature_session_task_id", "feature_session", ["task_id"])


def downgrade():
    for table in ("feature_session", "feature_message", "feature_scope"):
        op.drop_table(table)
