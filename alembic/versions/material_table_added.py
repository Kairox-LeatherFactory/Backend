"""add material_rate table (B-9: real material/accessory pricing → real FOB)

Revision ID: add_material_rate
Revises: <SET_TO_CURRENT_HEAD>          # TODO: `alembic heads`; resolve the two-root
                                        # split (b7d4f1a9c2e0 down_revision=None) first.
Create Date: 2026-07-10

Per-material (optionally per-supplier) unit price. BOM material/accessory lines are
born with unit_price 0 (left for manual entry); this table lets generation price them
so garment_fob_price = Σ line.total_cost rolls up a REAL FOB instead of overhead-only.
Runtime-editable, same posture as dxf_yield / cost_catalog.
"""
from alembic import op
import sqlalchemy as sa

revision = "add_material_rate"
down_revision = "20260704_client_checks"# TODO: set to current head
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "material_rate",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("material_key", sa.String(160), nullable=False, index=True),  # normalized line name
        sa.Column("display_name", sa.String(200)),
        sa.Column("uom", sa.String(20)),
        sa.Column("unit_price", sa.Numeric(12, 4), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False, server_default="USD"),
        sa.Column("supplier", sa.String(160)),
        sa.Column("is_current", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("material_key", "uom", "supplier", name="uq_material_rate"),
    )
    op.create_index("ix_material_rate_current", "material_rate", ["material_key", "is_current"])


def downgrade() -> None:
    op.drop_index("ix_material_rate_current", table_name="material_rate")
    op.drop_table("material_rate")