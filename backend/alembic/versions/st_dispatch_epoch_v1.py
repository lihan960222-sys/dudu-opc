"""Fence each employee's assignment and keep existing dispatches at version one."""
from alembic import op
import sqlalchemy as sa

revision = "st_dispatch_epoch_v1"
down_revision = "st_peer_reads_v1"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("feature_scope", "agent_job", "feature_session"):
        op.add_column(table, sa.Column("assignment_epoch", sa.Integer(), nullable=False, server_default="1"))


def downgrade():
    for table in ("feature_session", "agent_job", "feature_scope"):
        op.drop_column(table, "assignment_epoch")
