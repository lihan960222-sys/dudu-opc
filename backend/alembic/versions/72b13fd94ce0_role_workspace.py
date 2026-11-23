"""Bind a local role folder to its upstream agent."""
from alembic import op
import sqlalchemy as sa

revision = "72b13fd94ce0"
down_revision = "18c4d6a2f901"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agent", sa.Column("role_path", sa.String(), nullable=True))


def downgrade():
    op.drop_column("agent", "role_path")
