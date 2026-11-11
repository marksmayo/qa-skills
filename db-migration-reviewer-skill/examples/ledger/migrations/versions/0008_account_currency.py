"""currency per account

Revision ID: 0008
Revises: 0007
"""
from alembic import op
import sqlalchemy as sa

revision = '0008'
down_revision = '0007'


def upgrade():
    op.add_column('accounts', sa.Column('currency', sa.String(3), nullable=False, server_default='AUD'))


def downgrade():
    op.drop_column('accounts', 'currency')
