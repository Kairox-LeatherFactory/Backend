"""per-date order lines: sku_order_line

Revision ID: 20260713_0900_sku_order_line
Revises: 20260712_1000_piece_tracking
Create Date: 2026-07-13

The breakdown sheet lists the same (style, colour, size) across several dated
rows. The SKU stays ONE-per-triple (qty_ordered = SUM of its lines); each dated
row is preserved here for traceability. No unique constraint — two lines may
share a (sku_id, order_date) if the sheet repeats a triple on the same day; the
source_row keeps them distinguishable. FK ondelete CASCADE so clearing a client's
SKUs on re-import also clears its lines.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "20260713_0900_sku_order_line"
down_revision = "20260712_1000_piece_tracking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sku_order_line",
        sa.Column("sku_id", UUID(), nullable=False),
        sa.Column("order_date", sa.Date(), nullable=True),
        sa.Column("qty", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_row", sa.Integer(), nullable=True),
        sa.Column("id", UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["sku_id"], ["sku.id"],
                                name=op.f("fk_sku_order_line_sku_id_sku"),
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sku_order_line")),
    )
    op.create_index(op.f("ix_sku_order_line_sku_id"), "sku_order_line",
                    ["sku_id"], unique=False)
    op.create_index(op.f("ix_sku_order_line_order_date"), "sku_order_line",
                    ["order_date"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_sku_order_line_order_date"), table_name="sku_order_line")
    op.drop_index(op.f("ix_sku_order_line_sku_id"), table_name="sku_order_line")
    op.drop_table("sku_order_line")