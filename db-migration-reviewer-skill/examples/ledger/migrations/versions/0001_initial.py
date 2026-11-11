"""initial schema

Revision ID: 0001
Revises:
"""
from alembic import op
import sqlalchemy as sa

revision = '0001'
down_revision = None


def upgrade():
    op.create_table('accounts',
                    sa.Column('id', sa.Integer, primary_key=True),
                    sa.Column('name', sa.String(80), nullable=False),
                    sa.Column('legacy_code', sa.String(20)))
    op.create_table('transactions',
                    sa.Column('id', sa.Integer, primary_key=True),
                    sa.Column('account_id', sa.Integer, sa.ForeignKey('accounts.id'), nullable=False),
                    sa.Column('amount', sa.Numeric(12, 2), nullable=False))


def downgrade():
    op.drop_table('transactions')
    op.drop_table('accounts')
