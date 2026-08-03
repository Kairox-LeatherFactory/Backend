"""supplier_order: add thickness + dcm for PO-vs-received matching
 
Revision ID: supplier_order_spec
Revises: <CURRENT_HEAD>
Create Date: 2026-08-04
"""
import sqlalchemy as sa
from alembic import op
 
revision = "supplier_order_spec"
down_revision = "b17c0de0a001_order_context"    # <-- set to your latest head
branch_labels = None
depends_on = None
 
 
def upgrade() -> None:
    op.add_column("supplier_order",
                  sa.Column("thickness", sa.String(length=40), nullable=True))
    op.add_column("supplier_order",
                  sa.Column("dcm", sa.Numeric(14, 3), nullable=True))
 
 
def downgrade() -> None:
    op.drop_column("supplier_order", "dcm")
    op.drop_column("supplier_order", "thickness")