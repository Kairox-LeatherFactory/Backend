"""Add the material lot consumed quantity ledger.

Revision ID: 20260908_material_lot_used
Revises: 20260905_job_work
"""
from alembic import op
import sqlalchemy as sa

revision = "20260908_material_lot_used"
down_revision = "20260905_job_work"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "material_lot",
        sa.Column("used", sa.Numeric(14, 3), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("material_lot", "used")
