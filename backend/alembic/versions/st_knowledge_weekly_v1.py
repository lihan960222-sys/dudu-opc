"""Owner-granted, resource-bound weekly knowledge work and its unique dispatches."""
from alembic import op
import sqlalchemy as sa

revision = "st_knowledge_weekly_v1"
down_revision = "st_model_vendor_v1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("knowledge_weekly_grant",
        sa.Column("owner_id", sa.String(), primary_key=True), sa.Column("employee_id", sa.BigInteger(), primary_key=True),
        sa.Column("source_job_id", sa.BigInteger(), nullable=False), sa.Column("task_id", sa.BigInteger(), nullable=False),
        sa.Column("assignment_epoch", sa.Integer(), nullable=False), sa.Column("scope", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False), sa.Column("next_due", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generation", sa.String(32), nullable=False),
        sa.Column("blocked_reason", sa.String(300)))
    op.create_table("knowledge_weekly_slot",
        sa.Column("event_key", sa.String(90), primary_key=True), sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("employee_id", sa.BigInteger(), nullable=False), sa.Column("job_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("scope_digest", sa.String(64), nullable=False), sa.Column("cutoff", sa.BigInteger(), nullable=False),
        sa.Column("since", sa.BigInteger()))


def downgrade():
    op.drop_table("knowledge_weekly_slot")
    op.drop_table("knowledge_weekly_grant")
