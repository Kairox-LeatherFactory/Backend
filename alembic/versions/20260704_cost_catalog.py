"""config→DB: cost_catalog_line"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "20260704_cost_catalog"
down_revision = "20260704_dxf_yield_fabric_role"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        "cost_catalog_line",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column("garment_code", sa.String(40), nullable=False, index=True),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("uom", sa.String(20)),
        sa.Column("unit_price", sa.Numeric(12, 2)),
        sa.Column("qty_per_garment", sa.Numeric(12, 3), server_default="1"),
        sa.Column("sort_order", sa.Integer(), server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("garment_code", "category", "name", name="uq_cost_line"),
    )

def downgrade():
    op.drop_table("cost_catalog_line")