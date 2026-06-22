"""client_id nullable on spec_sheet / pattern_reference / style_consumption_template

A Stage-2 BOM is generated from the order + spec sheets BEFORE a client is resolved
(the client is read off the order sheet only at MD approval). Bom.client_id was already
made nullable for this; its satellite tables (spec_sheet, pattern_reference,
style_consumption_template) kept a NOT NULL client_id, which made the client-less
submission flow throw a NOT NULL violation on the first INSERT. Align them with
Bom.client_id.

Revision ID: c7a2d5b81f60
Revises: b1f3a2c9e7d4
Create Date: 2026-06-15 15:00:00.000000
"""
from alembic import op
import app.core.models  # GUID() custom type referenced in column defs

revision = 'c7a2d5b81f60'
down_revision = 'b1f3a2c9e7d4'
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column('spec_sheet', 'client_id',
                    existing_type=app.core.models.GUID(), nullable=True)
    op.alter_column('pattern_reference', 'client_id',
                    existing_type=app.core.models.GUID(), nullable=True)
    op.alter_column('style_consumption_template', 'client_id',
                    existing_type=app.core.models.GUID(), nullable=True)


def downgrade():
    # NOTE: will fail if any client-less rows exist; backfill before downgrading.
    op.alter_column('style_consumption_template', 'client_id',
                    existing_type=app.core.models.GUID(), nullable=False)
    op.alter_column('pattern_reference', 'client_id',
                    existing_type=app.core.models.GUID(), nullable=False)
    op.alter_column('spec_sheet', 'client_id',
                    existing_type=app.core.models.GUID(), nullable=False)
