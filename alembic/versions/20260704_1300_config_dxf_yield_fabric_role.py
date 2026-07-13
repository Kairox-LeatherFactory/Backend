"""config→DB: dxf_yield + fabric_role"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
revision = "20260704_1300_config_dxf_yield_fabric_role"
down_revision = "b7d4f1a9c2e0"   # or your current head
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        "dxf_yield",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column("species", sa.String(40), nullable=False, unique=True, index=True),
        sa.Column("factor", sa.Numeric(6, 3), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "fabric_role",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column("label", sa.String(120), nullable=False, unique=True, index=True),
        sa.Column("role", sa.String(30), nullable=False),
        sa.Column("category", sa.String(30), nullable=False),
        sa.Column("is_leather", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

def downgrade():
    op.drop_table("fabric_role")
    op.drop_table("dxf_yield")