"""Persist employee reporting lines and the owner's bounded steward hiring grant."""
from alembic import op
import sqlalchemy as sa

revision = "st_staff_hierarchy_v1"
down_revision = "feishu_lead_model_v1"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agent",sa.Column("manager_agent_id",sa.BigInteger(),nullable=True))
    op.add_column("agent",sa.Column("can_staff_team",sa.Boolean(),server_default=sa.false(),nullable=False))


def downgrade():
    op.drop_column("agent","can_staff_team")
    op.drop_column("agent","manager_agent_id")
