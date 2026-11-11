"""store amounts in cents

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa

revision = '0005'
down_revision = '0004'


def upgrade():
    op.alter_column('transactions', 'amount', new_column_name='amount_cents')
    op.alter_column('transactions', 'amount_cents', type_=sa.Integer(), existing_type=sa.Numeric(12, 2))


def downgrade():
    pass
