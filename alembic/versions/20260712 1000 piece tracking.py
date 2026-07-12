"""per-piece tracking: piece table + production_event.piece_id

Revision ID: 20260712_1000_piece_tracking
Revises: fiber_role_bom_item
Create Date: 2026-07-12

Adds the `piece` table (one physical garment, minted at CUTTING, carrying the
human-typeable `code` — the "bundle id" printed on its traveler card) and links
`production_event.piece_id` to it.

Safe to deploy with no data migration:
  * piece_id is NULLABLE, so every pre-existing (aggregate/legacy) production_event
    stays valid — nothing to backfill.
  * NO unique(piece_id, operation_id): rework is permitted (a piece may be logged
    at the same stage more than once); duplicates are surfaced in analytics, not
    rejected at the DB.

Column types mirror the baseline (postgresql UUID) so the FKs to sku.id /
operation.id / piece.id line up exactly. Runs on Postgres; the SQLite test harness
builds tables from the models via create_all and does not use this file.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "20260712_1000_piece_tracking"
down_revision = "fiber_role_bom_item"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "piece",
        sa.Column("code", sa.String(length=60), nullable=False),
        sa.Column("sku_id", UUID(), nullable=False),
        sa.Column("current_operation_id", UUID(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("id", UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["sku_id"], ["sku.id"],
                                name=op.f("fk_piece_sku_id_sku")),
        sa.ForeignKeyConstraint(["current_operation_id"], ["operation.id"],
                                name=op.f("fk_piece_current_operation_id_operation")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_piece")),
    )
    # `code` is the manually-typed lookup key -> unique + indexed.
    op.create_index(op.f("ix_piece_code"), "piece", ["code"], unique=True)
    op.create_index(op.f("ix_piece_sku_id"), "piece", ["sku_id"], unique=False)

    op.add_column(
        "production_event",
        sa.Column("piece_id", UUID(), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_production_event_piece_id_piece"),
        "production_event", "piece",
        ["piece_id"], ["id"],
    )
    op.create_index(
        op.f("ix_production_event_piece_id"),
        "production_event", ["piece_id"], unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_production_event_piece_id"), table_name="production_event")
    op.drop_constraint(
        op.f("fk_production_event_piece_id_piece"),
        "production_event", type_="foreignkey",
    )
    op.drop_column("production_event", "piece_id")
    op.drop_index(op.f("ix_piece_sku_id"), table_name="piece")
    op.drop_index(op.f("ix_piece_code"), table_name="piece")
    op.drop_table("piece")