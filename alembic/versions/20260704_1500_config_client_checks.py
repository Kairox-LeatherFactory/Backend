"""config→DB: client_check_rule"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
revision = "20260704_1500_config_client_checks"
down_revision = "20260704_1400_config_cost_catalog"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        "client_check_rule",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column("client_code", sa.String(60), nullable=False, index=True),
        sa.Column("rule_id", sa.String(60), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("severity", sa.String(10), server_default="warn"),
        sa.Column("field", sa.String(60)),
        sa.Column("range_lo", sa.Float()),
        sa.Column("range_hi", sa.Float()),
        sa.Column("params", sa.JSON()),
        sa.Column("sort_order", sa.Integer(), server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("client_code", "rule_id", name="uq_check_rule"),
    )

def downgrade():
    op.drop_table("client_check_rule")