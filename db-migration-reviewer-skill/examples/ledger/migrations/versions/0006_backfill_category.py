"""backfill the category

Revision ID: 0006
Revises: 0005
"""
from alembic import op

revision = '0006'
down_revision = '0005'


def upgrade():
    op.execute("UPDATE transactions SET category = 'general' WHERE category IS NULL")


def downgrade():
    pass
