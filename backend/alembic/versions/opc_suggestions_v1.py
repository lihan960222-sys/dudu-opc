"""Approval inbox for local steward observations."""
import sqlalchemy as sa
from alembic import op

from app.db.types import BigIntPK, UTCDateTime

revision = "opc_suggestions_v1"
down_revision = "st_feature_v1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("steward_suggestion",
        sa.Column("id", BigIntPK, sa.Identity(always=True), primary_key=True),
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("task_id", sa.BigInteger(), sa.ForeignKey("work_item.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_key", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("option", sa.String()), sa.Column("report", sa.Text()),
        sa.Column("followup_task_id", sa.BigInteger()),
        sa.Column("created_at", UTCDateTime, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("owner_id", "task_id", "source_key"), sqlite_autoincrement=True)
    op.create_index("ix_steward_suggestion_inbox", "steward_suggestion", ["owner_id", "state", "id"])


def downgrade():
    op.drop_table("steward_suggestion")
