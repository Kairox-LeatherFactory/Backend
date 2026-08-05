"""barcode_registry: denormalise order/sku/style context onto piece barcodes

Revision ID: b17c0de0a001
Revises: <PUT_YOUR_CURRENT_HEAD_HERE>
Create Date: 2026-08-01

WHY
    The barcode listing/history/analytics screens filter and group by order,
    style, sku and size. Without these columns every list query is a 4-table
    join (BarcodeRegistry -> Piece -> SKU -> Style -> ClientOrder). John Peter
    alone is ~1,273 piece rows; paginating that behind a 4-join per page is
    needless load. We denormalise order_id / sku_id / style_id onto the registry
    (all three are immutable for a piece once minted, so no update anomaly).

SAFETY
    * Columns are NULLABLE and the backfill is BATCHED, so the DDL applies
      instantly and the data fills in without a long table lock.
    * New reads tolerate NULL order_id and fall back to the join path, so the
      app is correct at every point during/after this migration.
    * premint.py is updated separately to populate these at mint time for all
      FUTURE uploads; this migration handles the EXISTING rows.
    * Portable SQLAlchemy only (no ::uuid / JSONB / ON CONFLICT) — Oracle-safe.
"""
from alembic import op
import sqlalchemy as sa

revision = "b17c0de0a001_order_context"
down_revision = "20260731_wage_line_uniq"  # <-- set to your current head before running
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. add nullable FK columns (instant)
    op.add_column("barcode_registry",
                  sa.Column("order_id", sa.CHAR(36) if False else sa.Uuid(), nullable=True))
    op.add_column("barcode_registry",
                  sa.Column("sku_id", sa.Uuid(), nullable=True))
    op.add_column("barcode_registry",
                  sa.Column("style_id", sa.Uuid(), nullable=True))

    op.create_foreign_key("fk_barcode_order", "barcode_registry",
                          "client_order", ["order_id"], ["id"])
    op.create_foreign_key("fk_barcode_sku", "barcode_registry",
                          "sku", ["sku_id"], ["id"])
    op.create_foreign_key("fk_barcode_style", "barcode_registry",
                          "style", ["style_id"], ["id"])

    op.create_index("ix_barcode_order_id", "barcode_registry", ["order_id"])
    op.create_index("ix_barcode_sku_id", "barcode_registry", ["sku_id"])
    op.create_index("ix_barcode_style_id", "barcode_registry", ["style_id"])
    # the hot path: "all PIECE barcodes for this order"
    op.create_index("ix_barcode_order_type", "barcode_registry",
                    ["order_id", "type"])

    # 2. BATCHED backfill for existing PIECE rows.
    #    Resolve order/sku/style via piece -> sku -> style -> client_order.
    conn = op.get_bind()
    meta = sa.MetaData()
    barcode = sa.Table("barcode_registry", meta, autoload_with=conn)
    piece = sa.Table("piece", meta, autoload_with=conn)
    sku = sa.Table("sku", meta, autoload_with=conn)
    style = sa.Table("style", meta, autoload_with=conn)

    # ids of piece-barcodes still missing context, oldest first
    todo = conn.execute(
        sa.select(barcode.c.id, barcode.c.piece_id)
        .where(barcode.c.type == "piece",
               barcode.c.piece_id.isnot(None),
               barcode.c.order_id.is_(None))
    ).fetchall()

    BATCH = 500
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        piece_ids = [r.piece_id for r in chunk]
        # one query resolves the whole chunk's context
        ctx = {
            row.piece_id: (row.order_id, row.sku_id, row.style_id)
            for row in conn.execute(
                sa.select(
                    piece.c.id.label("piece_id"),
                    style.c.client_order_id.label("order_id"),
                    sku.c.id.label("sku_id"),
                    style.c.id.label("style_id"),
                )
                .select_from(piece.join(sku, sku.c.id == piece.c.sku_id)
                                  .join(style, style.c.id == sku.c.style_id))
                .where(piece.c.id.in_(piece_ids))
            )
        }
        for r in chunk:
            oid, sid, stid = ctx.get(r.piece_id, (None, None, None))
            if oid is None:
                continue
            conn.execute(
                barcode.update()
                .where(barcode.c.id == r.id)
                .values(order_id=oid, sku_id=sid, style_id=stid)
            )


def downgrade() -> None:
    op.drop_index("ix_barcode_order_type", "barcode_registry")
    op.drop_index("ix_barcode_style_id", "barcode_registry")
    op.drop_index("ix_barcode_sku_id", "barcode_registry")
    op.drop_index("ix_barcode_order_id", "barcode_registry")
    op.drop_constraint("fk_barcode_style", "barcode_registry", type_="foreignkey")
    op.drop_constraint("fk_barcode_sku", "barcode_registry", type_="foreignkey")
    op.drop_constraint("fk_barcode_order", "barcode_registry", type_="foreignkey")
    op.drop_column("barcode_registry", "style_id")
    op.drop_column("barcode_registry", "sku_id")
    op.drop_column("barcode_registry", "order_id")