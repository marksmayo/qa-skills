"""budgets table with its index

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa

revision = '0003'
down_revision = '0002'


def upgrade():
    op.create_table('budgets',
                    sa.Column('id', sa.Integer, primary_key=True),
                    sa.Column('account_id', sa.Integer, nullable=False),
                    sa.Column('limit_cents', sa.Integer, nullable=False))
    op.create_index('ix_budgets_account', 'budgets', ['account_id'])


def downgrade():
    op.drop_index('ix_budgets_account', table_name='budgets')
    op.drop_table('budgets')
