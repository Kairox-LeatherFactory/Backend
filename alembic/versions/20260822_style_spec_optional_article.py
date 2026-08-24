"""Allow leather and lining specs without article or colour.

Leather and lining recipe lines are identified by their material kind and
thickness. Accessories continue to require an article at the service layer.

Revision ID: 20260822_style_spec_optional_article
Revises: 20260822_style_delivery
"""
from alembic import op


revision = "20260822_style_spec_optional_article"
down_revision = "20260822_style_delivery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("style_material_spec", "article", nullable=True)


def downgrade() -> None:
    op.alter_column("style_material_spec", "article", nullable=False)
