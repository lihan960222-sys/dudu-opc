"""Explicit employee model vendor and owner-authorized delegation of project writes."""
from alembic import op
import sqlalchemy as sa

revision = "st_model_vendor_v1"
down_revision = "st_dispatch_epoch_v1"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agent", sa.Column("model_vendor", sa.String(64), nullable=True))
    op.add_column("agent", sa.Column("delegate_project_writes", sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade():
    op.drop_column("agent", "delegate_project_writes")
    op.drop_column("agent", "model_vendor")
