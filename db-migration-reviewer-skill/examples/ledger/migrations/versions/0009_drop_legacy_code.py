"""drop accounts.legacy_code (excluded from the model since release 2.3)

Revision ID: 0009
Revises: 0008
"""
from alembic import op
import sqlalchemy as sa

revision = '0009'
down_revision = '0008'


def upgrade():
    op.drop_column('accounts', 'legacy_code')


def downgrade():
    op.add_column('accounts', sa.Column('legacy_code', sa.String(20)))
