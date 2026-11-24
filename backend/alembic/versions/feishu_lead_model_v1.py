"""Keep a distinct steward conversation per selected engine/model."""
from alembic import op
import sqlalchemy as sa

revision='feishu_lead_model_v1'
down_revision='st_project_consult_v1'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('feishu_lead_chat',
        sa.Column('thread_id',sa.BigInteger(),sa.ForeignKey('thread.id',ondelete='CASCADE'),primary_key=True,autoincrement=False),
        sa.Column('channel',sa.String(64),nullable=False),sa.Column('selection',sa.String(64),nullable=False),
        sa.Column('model',sa.String()),sa.Column('project',sa.String(),nullable=False),
        sa.Column('isolated',sa.Boolean(),nullable=False),
        sa.UniqueConstraint('channel','selection',name='uq_feishu_lead_selection'))


def downgrade():
    op.drop_table('feishu_lead_chat')
