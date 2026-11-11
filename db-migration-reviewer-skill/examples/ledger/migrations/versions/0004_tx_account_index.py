"""index transactions by account

Revision ID: 0004
Revises: 0003
"""
from alembic import op

revision = '0004'
down_revision = '0003'


def upgrade():
    op.create_index('ix_transactions_account', 'transactions', ['account_id'])


def downgrade():
    op.drop_index('ix_transactions_account', table_name='transactions')
