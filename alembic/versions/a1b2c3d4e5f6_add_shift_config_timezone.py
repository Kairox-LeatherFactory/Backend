"""add shift_config.timezone

Stores the factory's IANA timezone (e.g. 'Asia/Kolkata') as the single source
of truth for wall-clock interpretation (is_late) and the work_date boundary.
Timestamps stay UTC; this is applied only at the business-logic/display edges.

Revision ID: a1b2c3d4e5f6
Revises: d39fcd6fa318
Create Date: 2026-06-03 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'a1b2c3d4e5f6'
down_revision = 'd39fcd6fa318'
branch_labels = None
depends_on = None


def upgrade():
    # server_default backfills the existing singleton row; the model carries the
    # same default for app-level inserts.
    op.add_column(
        'shift_config',
        sa.Column('timezone', sa.String(length=64), nullable=False,
                  server_default='Asia/Kolkata'),
    )


def downgrade():
    op.drop_column('shift_config', 'timezone')
