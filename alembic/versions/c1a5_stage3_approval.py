"""stage 3 — approval: bom rejection + PDF-export columns

Additive `bom` columns for the Stage-3 approval stage (stage-3 spec §1c):

  rejected_by / rejected_at / rejection_reason   the MD-only reject path (reason
                                                 also copied into the BOM_REJECT audit)
  export_document_id / exported_at               the rendered BOM-quote PDF
                                                 (Document.kind=bom_quote) + when

No enum migration: BomStatus is a plain VARCHAR storing the enum `.value` (the
module convention), so the new states (ready_for_review / rejected / exported) need
no `ALTER TYPE`. Notifications + audit_log already exist (Stage 0) — unchanged.

Reversible: downgrade drops the five columns. Plain DDL only.

Revision ID: c1a5_stage3_approval
Revises: c1a4_stage2_bom_generation
Create Date: 2026-06-12
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "c1a5_stage3_approval"
down_revision = "c1a4_stage2_bom_generation"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("bom", sa.Column("rejected_by", UUID(as_uuid=True),
                                   sa.ForeignKey("app_user.id"), nullable=True))
    op.add_column("bom", sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("bom", sa.Column("rejection_reason", sa.Text(), nullable=True))
    op.add_column("bom", sa.Column("export_document_id", UUID(as_uuid=True),
                                   sa.ForeignKey("document.id"), nullable=True))
    op.add_column("bom", sa.Column("exported_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("bom", "exported_at")
    op.drop_column("bom", "export_document_id")
    op.drop_column("bom", "rejection_reason")
    op.drop_column("bom", "rejected_at")
    op.drop_column("bom", "rejected_by")
