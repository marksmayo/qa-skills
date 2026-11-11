"""add a category to transactions

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'


def upgrade():
    op.add_column('transactions', sa.Column('category', sa.String(40), nullable=True))


def downgrade():
    op.drop_column('transactions', 'category')
