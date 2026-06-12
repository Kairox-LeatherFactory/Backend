"""stage 1 — upload & validation: submission + client_template + document columns

Adds the Stage-1 (upload & validation) schema deltas on top of the Stage-0
procurement schema:

  1. `submission` — the upload-batch / PAIRING row. The first upload mints a
     submission_id; the second references it. order/spec slots point at `document`;
     `client_order_id` is filled later in Stage 2 (resolves "paired by order_id"
     once the order actually exists). The two slot FKs use ALTER (added last) to
     break the circular dependency with `document.submission_id`.

  2. `document` columns — submission_id + the validation/scan result fields the API
     envelope returns (validation_status, classified_kind/spec_type, method,
     confidence, client_match_code, scan_status, validation_signals JSONB).
     validation_status/scan_status carry server defaults so existing rows backfill.

  3. `client_template` — the config-driven validation registry (§4), seeded
     idempotently from config/client_templates.yaml by scripts/seed.py.

Reversible: downgrade drops the columns/tables. Plain DDL only — the registry data
backfill lives in the seed, never in Alembic (stage-0 §3).

Revision ID: c1a3_stage1_upload_validation
Revises: c1a2_drop_doc_order_fk
Create Date: 2026-06-11
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "c1a3_stage1_upload_validation"
down_revision = "c1a2_drop_doc_order_fk"
branch_labels = None
depends_on = None


def _ts_cols():
    return (
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
    )


def upgrade():
    # ── 1. submission (slot FKs added later to break the circular dependency) ─
    op.create_table(
        "submission",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("client_id", UUID(as_uuid=True),
                  sa.ForeignKey("client.id"), nullable=True),
        sa.Column("created_by", UUID(as_uuid=True),
                  sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("order_document_id", UUID(as_uuid=True), nullable=True),
        sa.Column("spec_document_id", UUID(as_uuid=True), nullable=True),
        sa.Column("client_order_id", UUID(as_uuid=True),
                  sa.ForeignKey("client_order.id"), nullable=True),
        *_ts_cols(),
    )
    op.create_index("ix_submission_client_id", "submission", ["client_id"])
    op.create_index("ix_submission_created_by", "submission", ["created_by"])
    op.create_index("ix_submission_status", "submission", ["status"])
    op.create_index("ix_submission_client_order_id", "submission", ["client_order_id"])

    # ── 2. document validation columns ───────────────────────────────────────
    op.add_column("document", sa.Column("submission_id", UUID(as_uuid=True),
                                        sa.ForeignKey("submission.id"), nullable=True))
    op.add_column("document", sa.Column("size_bytes", sa.Integer(), nullable=True))
    op.add_column("document", sa.Column("validation_status", sa.String(25),
                                        nullable=False, server_default="pending"))
    op.add_column("document", sa.Column("classified_kind", sa.String(30), nullable=True))
    op.add_column("document", sa.Column("classified_spec_type", sa.String(30), nullable=True))
    op.add_column("document", sa.Column("classification_method", sa.String(20), nullable=True))
    op.add_column("document", sa.Column("classification_confidence", sa.Numeric(5, 4), nullable=True))
    op.add_column("document", sa.Column("client_match_code", sa.String(60), nullable=True))
    op.add_column("document", sa.Column("scan_status", sa.String(20),
                                        nullable=False, server_default="skipped"))
    op.add_column("document", sa.Column("validation_signals", JSONB(), nullable=True))
    op.create_index("ix_document_submission_id", "document", ["submission_id"])
    op.create_index("ix_document_validation_status", "document", ["validation_status"])

    # ── 3. client_template registry ──────────────────────────────────────────
    op.create_table(
        "client_template",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("client_code", sa.String(60), nullable=False),
        sa.Column("display_name", sa.String(160), nullable=True),
        sa.Column("doc_kind", sa.String(30), nullable=False),
        sa.Column("language", sa.String(10), nullable=True),
        sa.Column("size_system", sa.String(10), nullable=True),
        sa.Column("currency", sa.String(3), nullable=True),
        sa.Column("expected_layout", sa.String(20), nullable=True),
        sa.Column("spec_type_hint", sa.String(30), nullable=True),
        sa.Column("accepted_mime", JSONB(), nullable=True),
        sa.Column("anchors", JSONB(), nullable=True),
        sa.Column("fingerprints", JSONB(), nullable=True),
        sa.Column("grid_signals", JSONB(), nullable=True),
        sa.Column("thresholds", JSONB(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_ts_cols(),
        sa.UniqueConstraint("client_code", "doc_kind",
                            name="uq_client_template_code_kind"),
    )
    op.create_index("ix_client_template_client_code", "client_template", ["client_code"])
    op.create_index("ix_client_template_doc_kind", "client_template", ["doc_kind"])
    op.create_index("ix_client_template_is_active", "client_template", ["is_active"])

    # ── 4. submission slot FKs (now that document exists w/ submission_id) ────
    op.create_foreign_key("fk_submission_order_document", "submission", "document",
                          ["order_document_id"], ["id"])
    op.create_foreign_key("fk_submission_spec_document", "submission", "document",
                          ["spec_document_id"], ["id"])


def downgrade():
    op.drop_constraint("fk_submission_order_document", "submission", type_="foreignkey")
    op.drop_constraint("fk_submission_spec_document", "submission", type_="foreignkey")

    op.drop_index("ix_client_template_is_active", table_name="client_template")
    op.drop_index("ix_client_template_doc_kind", table_name="client_template")
    op.drop_index("ix_client_template_client_code", table_name="client_template")
    op.drop_table("client_template")

    op.drop_index("ix_document_validation_status", table_name="document")
    op.drop_index("ix_document_submission_id", table_name="document")
    for col in ("validation_signals", "scan_status", "client_match_code",
                "classification_confidence", "classification_method",
                "classified_spec_type", "classified_kind", "validation_status",
                "size_bytes", "submission_id"):
        op.drop_column("document", col)

    op.drop_index("ix_submission_client_order_id", table_name="submission")
    op.drop_index("ix_submission_status", table_name="submission")
    op.drop_index("ix_submission_created_by", table_name="submission")
    op.drop_index("ix_submission_client_id", table_name="submission")
    op.drop_table("submission")
