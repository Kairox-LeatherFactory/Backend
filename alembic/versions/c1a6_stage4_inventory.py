"""stage 4 — inventory: reservation ledger, alias + uom reference, check-line deltas

Adds the Stage-4 (inventory check) schema on top of Stage 3 (stage-4 spec §10). The
base tables `inventory_item` / `inventory_check` / `inventory_check_line` already exist
(c1a1); this adds:

  1. `inventory_reservation`     the SOFT allocation ledger (§3) — available =
                                 qty_on_hand − Σ active reservations; never mutates
                                 qty_on_hand, so a master re-sync can snapshot-replace
                                 stock without destroying a commitment.
  2. `material_alias`            curated BOM-term → inventory-key synonyms (§5.2),
                                 config-seeded; SHEEP GLASS → SHEEP NAPPA.
  3. `uom_conversion`            stock-UOM → BOM-UOM factors (§6.2); identity rows
                                 (dm²↔DCM, pc↔NOS) seeded.
  4. `inventory_check_line`      + matched_method (MatchMethod) + flags JSONB (§5/§10).

No enum migration: ReservationStatus / MatchMethod are plain VARCHARs storing the enum
`.value` (the module convention), so no `ALTER TYPE`. New audit action
`INVENTORY_CHECK_RUN` is a string — no schema change to audit_log.

Reversible: downgrade drops the columns/tables. Plain DDL only — the inventory master
load + the alias/uom seeds live in the importer/seed, never in Alembic.

Revision ID: c1a6_stage4_inventory
Revises: c1a5_stage3_approval
Create Date: 2026-06-12
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "c1a6_stage4_inventory"
down_revision = "c1a5_stage3_approval"
branch_labels = None
depends_on = None


def _ts_cols():
    return (
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
    )


def upgrade():
    # ── 1. inventory_reservation (the soft allocation ledger) ─────────────────
    op.create_table(
        "inventory_reservation",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("inventory_item_id", UUID(as_uuid=True),
                  sa.ForeignKey("inventory_item.id"), nullable=False),
        sa.Column("bom_id", UUID(as_uuid=True), sa.ForeignKey("bom.id"), nullable=False),
        sa.Column("inventory_check_line_id", UUID(as_uuid=True),
                  sa.ForeignKey("inventory_check_line.id"), nullable=True),
        sa.Column("qty", sa.Numeric(12, 3), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_reason", sa.String(120), nullable=True),
        *_ts_cols(),
    )
    op.create_index("ix_inventory_reservation_inventory_item_id",
                    "inventory_reservation", ["inventory_item_id"])
    op.create_index("ix_inventory_reservation_bom_id", "inventory_reservation", ["bom_id"])
    op.create_index("ix_inventory_reservation_status", "inventory_reservation", ["status"])

    # ── 2. material_alias ──────────────────────────────────────────────────────
    op.create_table(
        "material_alias",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("bom_term", sa.String(200), nullable=False),
        sa.Column("inventory_key", sa.String(200), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_ts_cols(),
        sa.UniqueConstraint("bom_term", name="uq_material_alias_term"),
    )
    op.create_index("ix_material_alias_bom_term", "material_alias", ["bom_term"])
    op.create_index("ix_material_alias_inventory_key", "material_alias", ["inventory_key"])

    # ── 3. uom_conversion ──────────────────────────────────────────────────────
    op.create_table(
        "uom_conversion",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("from_uom", sa.String(20), nullable=False),
        sa.Column("to_uom", sa.String(20), nullable=False),
        sa.Column("factor", sa.Numeric(16, 6), nullable=False, server_default="1"),
        *_ts_cols(),
        sa.UniqueConstraint("from_uom", "to_uom", name="uq_uom_conversion_pair"),
    )
    op.create_index("ix_uom_conversion_from_uom", "uom_conversion", ["from_uom"])
    op.create_index("ix_uom_conversion_to_uom", "uom_conversion", ["to_uom"])

    # ── 4. inventory_check_line deltas ─────────────────────────────────────────
    op.add_column("inventory_check_line", sa.Column("matched_method", sa.String(20), nullable=True))
    op.add_column("inventory_check_line", sa.Column("flags", JSONB(), nullable=True))


def downgrade():
    op.drop_column("inventory_check_line", "flags")
    op.drop_column("inventory_check_line", "matched_method")

    op.drop_index("ix_uom_conversion_to_uom", table_name="uom_conversion")
    op.drop_index("ix_uom_conversion_from_uom", table_name="uom_conversion")
    op.drop_table("uom_conversion")

    op.drop_index("ix_material_alias_inventory_key", table_name="material_alias")
    op.drop_index("ix_material_alias_bom_term", table_name="material_alias")
    op.drop_table("material_alias")

    op.drop_index("ix_inventory_reservation_status", table_name="inventory_reservation")
    op.drop_index("ix_inventory_reservation_bom_id", table_name="inventory_reservation")
    op.drop_index("ix_inventory_reservation_inventory_item_id", table_name="inventory_reservation")
    op.drop_table("inventory_reservation")
