"""category is required

Revision ID: 0007
Revises: 0006
"""
from alembic import op
import sqlalchemy as sa

revision = '0007'
down_revision = '0006'


def upgrade():
    op.alter_column('transactions', 'category', existing_type=sa.String(40), nullable=False)


def downgrade():
    op.alter_column('transactions', 'category', existing_type=sa.String(40), nullable=True)
