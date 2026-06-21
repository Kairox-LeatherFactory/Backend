"""drop unique constraint on document.sha256 (testing phase)
 
Revision ID: f3a9c1d2e8b7
Revises: c7a2d5b81f60
Create Date: 2026-06-21 12:00:00.000000
"""
from alembic import op
 
# revision identifiers, used by Alembic.
revision = "f3a9c1d2e8b7"
down_revision = "c7a2d5b81f60"
branch_labels = None
depends_on = None
 
 
def upgrade() -> None:
    # sha256 uniqueness is enforced via a UNIQUE INDEX (ix_document_sha256), not a
    # table-level CONSTRAINT — confirmed via:
    #   SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'document';
    # Drop the unique index and recreate it as a plain (non-unique) index so lookups
    # by sha256 stay fast, but byte-identical re-uploads no longer collide.
    op.drop_index("ix_document_sha256", table_name="document")
    op.create_index("ix_document_sha256", "document", ["sha256"], unique=False)
 
 
def downgrade() -> None:
    op.drop_index("ix_document_sha256", table_name="document")
    op.create_index("ix_document_sha256", "document", ["sha256"], unique=True)