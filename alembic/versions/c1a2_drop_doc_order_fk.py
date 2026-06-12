"""drop document.client_order_id (redundant, removes circular FK)

A `document` (order sheet / spec sheet) is uploaded at Stage 1, BEFORE any order
exists — the `client_order` is *derived from* the order sheet during Stage-2 AI
extraction. So `document.client_order_id` was always NULL at insert and pointed
the wrong way; the real link is `client_order.source_document_id` → document.
Dropping it also removes the circular FK between `document` and `client_order`.

Safe: the column is brand-new and empty (0 rows) at the time of this migration.

NOTE: revision IDs must fit `alembic_version.version_num` (varchar(32)), hence
the short id.

Revision ID: c1a2_drop_doc_order_fk
Revises: c1a1_procurement_client_order
Create Date: 2026-06-11
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "c1a2_drop_doc_order_fk"
down_revision = "c1a1_procurement_client_order"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("fk_document_client_order_id_client_order",
                       "document", type_="foreignkey")
    op.drop_index("ix_document_client_order_id", table_name="document")
    op.drop_column("document", "client_order_id")


def downgrade():
    op.add_column("document", sa.Column("client_order_id", UUID(as_uuid=True), nullable=True))
    op.create_index("ix_document_client_order_id", "document", ["client_order_id"])
    op.create_foreign_key("fk_document_client_order_id_client_order",
                          "document", "client_order", ["client_order_id"], ["id"])
