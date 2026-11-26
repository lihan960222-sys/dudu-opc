"""Owner-granted employee read access to active peer workspaces."""
from alembic import op
import sqlalchemy as sa

revision = "st_peer_reads_v1"
down_revision = "st_staff_hierarchy_v1"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("agent",sa.Column("team_workspaces_readable",sa.Boolean(),nullable=False,server_default=sa.false()))

def downgrade():
    op.drop_column("agent","team_workspaces_readable")
