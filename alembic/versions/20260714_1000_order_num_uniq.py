"""client_order.order_number globally unique

Revision ID: 20260714_1000_order_num_uniq
Revises: 20260713_0900_sku_order_line
"""
from alembic import op

revision = "20260713_0900_sku_order_line"   # 29 chars, fits
down_revision = "20260713_0900_sku_order_line"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_client_order_order_number", table_name="client_order")
    op.create_unique_constraint(
        "uq_client_order_order_number", "client_order", ["order_number"])


def downgrade() -> None:
    op.drop_constraint("uq_client_order_order_number", "client_order",
                       type_="unique")
    op.create_index("ix_client_order_order_number", "client_order",
                    ["order_number"], unique=False)