"""Create the four BOM-intake tables no revision ever created.

order_style, order_style_color, order_extraction and spec_extraction exist in
app/modules/bom/models.py, but no migration in this chain created them. The
live databases have them only because `DEBUG=true` makes the app's lifespan run
`Base.metadata.create_all` on startup. A brand-new database built purely by
`alembic upgrade head` therefore died at 20260911_order_style_fk with
`relation "order_style" does not exist`.

IDEMPOTENT ON PURPOSE: every table is created only if it is missing, so
databases that already got these tables from create_all pass through untouched.
(Those databases are already past this revision anyway; Alembic never runs a
revision inserted behind the current head.)

HISTORICAL SHAPE: order_style.pattern_reference_id is created pointing at
pattern_reference — what it pointed at before 20260911_order_style_fk — so
that revision's repoint to pattern_extraction still means something.

Revision ID: 20260910_bom_intake_tables
Revises: 20260908_material_lot_used
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260910_bom_intake_tables"
down_revision = "20260908_material_lot_used"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def _base_cols():
    return [
        sa.Column("id", UUID, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
    ]


def _indexes(table, cols):
    for col in cols:
        op.create_index(f"ix_{table}_{col}", table, [col], if_not_exists=True)


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())

    if "order_extraction" not in existing:
        op.create_table(
            "order_extraction",
            sa.Column("source_document_id", UUID, nullable=True),
            sa.Column("extracted_by", sa.String(20), nullable=False),
            sa.Column("confidence_overall", sa.Numeric(5, 4), nullable=True),
            sa.Column("raw_payload", JSONB, nullable=False),
            sa.Column("order_number", sa.String(120), nullable=True),
            sa.Column("style_no", sa.String(120), nullable=True),
            sa.Column("client_name", sa.String(120), nullable=True),
            sa.Column("season", sa.String(20), nullable=True),
            sa.Column("currency", sa.String(3), nullable=True),
            sa.Column("order_qty", sa.Integer(), nullable=False),
            sa.Column("num_lines", sa.Integer(), nullable=False),
            sa.Column("num_warnings", sa.Integer(), nullable=False),
            sa.Column("manual_entry_required", sa.Boolean(), nullable=False),
            sa.Column("promoted_to_bom_id", UUID, nullable=True),
            sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
            *_base_cols(),
            sa.ForeignKeyConstraint(["promoted_to_bom_id"], ["bom.id"],
                                    name="fk_order_extraction_promoted_to_bom_id_bom",
                                    ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["source_document_id"], ["document.id"],
                                    name="fk_order_extraction_source_document_id_document",
                                    ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id", name="pk_order_extraction"),
        )
    _indexes("order_extraction", ["extracted_by", "manual_entry_required",
                                  "order_number", "promoted_to_bom_id",
                                  "source_document_id", "style_no"])

    if "order_style" not in existing:
        op.create_table(
            "order_style",
            sa.Column("client_id", UUID, nullable=True),
            sa.Column("submission_id", UUID, nullable=False),
            sa.Column("style_signature", sa.String(120), nullable=False),
            sa.Column("style_name", sa.String(200), nullable=False),
            sa.Column("material", sa.String(200), nullable=True),
            sa.Column("qty", sa.Integer(), nullable=False),
            sa.Column("per_size_qty", sa.JSON(), nullable=False),
            sa.Column("warnings", sa.JSON(), nullable=False),
            sa.Column("spec_document_id", UUID, nullable=True),
            sa.Column("spec_match_status", sa.String(20), nullable=False),
            sa.Column("pattern_reference_id", UUID, nullable=True),
            sa.Column("dxf_match_status", sa.String(20), nullable=False),
            sa.Column("bom_id", UUID, nullable=True),
            *_base_cols(),
            sa.ForeignKeyConstraint(["bom_id"], ["bom.id"],
                                    name="fk_order_style_bom_id_bom", ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["client_id"], ["client.id"],
                                    name="fk_order_style_client_id_client",
                                    ondelete="SET NULL"),
            # pre-20260911 target; that revision repoints it at pattern_extraction
            sa.ForeignKeyConstraint(["pattern_reference_id"], ["pattern_reference.id"],
                                    name="fk_order_style_pattern_reference_id_pattern_reference",
                                    ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["spec_document_id"], ["document.id"],
                                    name="fk_order_style_spec_document_id_document",
                                    ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["submission_id"], ["submission.id"],
                                    name="fk_order_style_submission_id_submission"),
            sa.PrimaryKeyConstraint("id", name="pk_order_style"),
            sa.UniqueConstraint("submission_id", "style_signature",
                                name="uq_order_style_submission_style"),
        )
    _indexes("order_style", ["client_id", "style_signature", "submission_id"])

    if "spec_extraction" not in existing:
        op.create_table(
            "spec_extraction",
            sa.Column("source_document_id", UUID, nullable=True),
            sa.Column("extracted_by", sa.String(20), nullable=False),
            sa.Column("confidence_overall", sa.Numeric(5, 4), nullable=True),
            sa.Column("raw_payload", JSONB, nullable=False),
            sa.Column("style_no", sa.String(120), nullable=True),
            sa.Column("client_name", sa.String(120), nullable=True),
            sa.Column("garment_type_guess", sa.String(60), nullable=True),
            sa.Column("num_measurements", sa.Integer(), nullable=False),
            sa.Column("num_accessories", sa.Integer(), nullable=False),
            sa.Column("num_warnings", sa.Integer(), nullable=False),
            sa.Column("manual_entry_required", sa.Boolean(), nullable=False),
            sa.Column("promoted_to_spec_sheet_id", UUID, nullable=True),
            sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
            *_base_cols(),
            sa.ForeignKeyConstraint(["promoted_to_spec_sheet_id"], ["spec_sheet.id"],
                                    name="fk_spec_extraction_promoted_to_spec_sheet_id_spec_sheet",
                                    ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["source_document_id"], ["document.id"],
                                    name="fk_spec_extraction_source_document_id_document",
                                    ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id", name="pk_spec_extraction"),
        )
    _indexes("spec_extraction", ["extracted_by", "manual_entry_required",
                                 "promoted_to_spec_sheet_id", "source_document_id",
                                 "style_no"])

    if "order_style_color" not in existing:
        op.create_table(
            "order_style_color",
            sa.Column("order_style_id", UUID, nullable=False),
            sa.Column("color_key", sa.String(80), nullable=False),
            sa.Column("color_label", sa.String(200), nullable=False),
            sa.Column("qty", sa.Integer(), nullable=False),
            sa.Column("per_size_qty", sa.JSON(), nullable=False),
            sa.Column("warnings", sa.JSON(), nullable=False),
            *_base_cols(),
            sa.ForeignKeyConstraint(["order_style_id"], ["order_style.id"],
                                    name="fk_order_style_color_order_style_id_order_style"),
            sa.PrimaryKeyConstraint("id", name="pk_order_style_color"),
            sa.UniqueConstraint("order_style_id", "color_key", name="uq_order_style_color"),
        )
    _indexes("order_style_color", ["order_style_id"])


def downgrade() -> None:
    for table in ("order_style_color", "spec_extraction", "order_style",
                  "order_extraction"):
        op.drop_table(table, if_exists=True)
