"""stage 2 — BOM generation: POM/DCM memory tables + bom/bom_item deltas

Adds the Stage-2 (AI BOM generation) schema on top of Stage 1 (§3 of the spec):

  1. `garment_type`               required POMs + the Source-3 area heuristic per type
  2. `pom_dictionary`             native term → standard pom_code (config-seeded)
  3. `pom_measurement`            standardized POMs per spec sheet, per size
  4. `style_consumption_template` the DCM memory — CROSS-ORDER-keyed on a stable
                                  style_signature (NOT style_id), back-filled by the
                                  §10 confirmation gate so a 2nd order is a template hit
  5. `pattern_reference`          "follow existing pattern X" (both real clients)
  6. `bom_item` deltas            dcm_source + dcm_confidence (null on non-material lines)
  7. `bom` deltas                 cutting_confirmed_by/_at (the §10 gate) + garment_type_id
                                  + dcm_base_size (DCM-memory provenance so confirm keys
                                  the template identically to generation — §11.5)

Reversible: downgrade drops the columns/tables. Plain DDL only — the garment-type +
POM-dictionary backfill lives in the seed (seed_stage2.py), never in Alembic.

Revision ID: c1a4_stage2_bom_generation
Revises: c1a3_stage1_upload_validation
Create Date: 2026-06-12
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "c1a4_stage2_bom_generation"
down_revision = "c1a3_stage1_upload_validation"
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
    # ── 1. garment_type (FK'd by pom_dictionary, style_consumption_template, bom) ─
    op.create_table(
        "garment_type",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("code", sa.String(60), nullable=False),
        sa.Column("label", sa.String(120), nullable=True),
        sa.Column("required_poms", JSONB(), nullable=True),
        sa.Column("area_formula", JSONB(), nullable=True),
        sa.Column("default_wastage_pct", sa.Numeric(5, 2), nullable=True),
        *_ts_cols(),
        sa.UniqueConstraint("code", name="uq_garment_type_code"),
    )
    op.create_index("ix_garment_type_code", "garment_type", ["code"])

    # ── 2. pom_dictionary ─────────────────────────────────────────────────────
    op.create_table(
        "pom_dictionary",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("language", sa.String(10), nullable=False),
        sa.Column("source_term", sa.String(160), nullable=False),
        sa.Column("pom_code", sa.String(40), nullable=False),
        sa.Column("garment_type_id", UUID(as_uuid=True),
                  sa.ForeignKey("garment_type.id"), nullable=True),
        sa.Column("weight", sa.Integer(), nullable=False, server_default="1"),
        *_ts_cols(),
        sa.UniqueConstraint("language", "source_term", "garment_type_id",
                            name="uq_pom_dictionary_term"),
    )
    op.create_index("ix_pom_dictionary_language", "pom_dictionary", ["language"])
    op.create_index("ix_pom_dictionary_source_term", "pom_dictionary", ["source_term"])
    op.create_index("ix_pom_dictionary_pom_code", "pom_dictionary", ["pom_code"])

    # ── 3. pom_measurement ────────────────────────────────────────────────────
    op.create_table(
        "pom_measurement",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("spec_sheet_id", UUID(as_uuid=True),
                  sa.ForeignKey("spec_sheet.id"), nullable=False),
        sa.Column("size", sa.String(10), nullable=False),
        sa.Column("pom_code", sa.String(40), nullable=False),
        sa.Column("value", sa.Numeric(10, 2), nullable=True),
        sa.Column("pitch", sa.Numeric(6, 2), nullable=True),
        sa.Column("tolerance", JSONB(), nullable=True),
        sa.Column("source_term", sa.String(160), nullable=True),
        sa.Column("extracted_by", sa.String(20), nullable=True),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=True),
        *_ts_cols(),
        sa.UniqueConstraint("spec_sheet_id", "size", "pom_code",
                            name="uq_pom_measurement_identity"),
    )
    op.create_index("ix_pom_measurement_spec_sheet_id", "pom_measurement", ["spec_sheet_id"])

    # ── 4. style_consumption_template (the DCM memory) ───────────────────────
    op.create_table(
        "style_consumption_template",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("client_id", UUID(as_uuid=True), sa.ForeignKey("client.id"), nullable=False),
        sa.Column("style_signature", sa.String(120), nullable=False),
        sa.Column("garment_type_id", UUID(as_uuid=True),
                  sa.ForeignKey("garment_type.id"), nullable=True),
        sa.Column("material_category", sa.String(20), nullable=False),
        sa.Column("size", sa.String(10), nullable=False),
        sa.Column("dcm_value", sa.Numeric(12, 3), nullable=False),
        sa.Column("uom", sa.String(20), nullable=True),
        sa.Column("confirmed_by", UUID(as_uuid=True), sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_bom_id", UUID(as_uuid=True), sa.ForeignKey("bom.id"), nullable=True),
        *_ts_cols(),
        sa.UniqueConstraint("client_id", "style_signature", "garment_type_id",
                            "material_category", "size",
                            name="uq_style_consumption_identity"),
    )
    op.create_index("ix_style_consumption_client_id", "style_consumption_template", ["client_id"])
    op.create_index("ix_style_consumption_signature", "style_consumption_template",
                    ["style_signature"])

    # ── 5. pattern_reference ──────────────────────────────────────────────────
    op.create_table(
        "pattern_reference",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("client_id", UUID(as_uuid=True), sa.ForeignKey("client.id"), nullable=False),
        sa.Column("spec_sheet_id", UUID(as_uuid=True),
                  sa.ForeignKey("spec_sheet.id"), nullable=True),
        sa.Column("pattern_code", sa.String(120), nullable=False),
        sa.Column("base_size", sa.String(10), nullable=True),
        sa.Column("resolved_style_id", UUID(as_uuid=True),
                  sa.ForeignKey("style.id"), nullable=True),
        sa.Column("resolved_template_id", UUID(as_uuid=True),
                  sa.ForeignKey("style_consumption_template.id"), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        *_ts_cols(),
    )
    op.create_index("ix_pattern_reference_client_id", "pattern_reference", ["client_id"])
    op.create_index("ix_pattern_reference_spec_sheet_id", "pattern_reference", ["spec_sheet_id"])
    op.create_index("ix_pattern_reference_pattern_code", "pattern_reference", ["pattern_code"])

    # ── 6. bom_item DCM provenance ────────────────────────────────────────────
    op.add_column("bom_item", sa.Column("dcm_source", sa.String(20), nullable=True))
    op.add_column("bom_item", sa.Column("dcm_confidence", sa.Numeric(5, 4), nullable=True))

    # ── 7. bom: the §10 gate + DCM-memory provenance ─────────────────────────
    op.add_column("bom", sa.Column("cutting_confirmed_by", UUID(as_uuid=True),
                                   sa.ForeignKey("app_user.id"), nullable=True))
    op.add_column("bom", sa.Column("cutting_confirmed_at", sa.DateTime(timezone=True),
                                   nullable=True))
    op.add_column("bom", sa.Column("garment_type_id", UUID(as_uuid=True),
                                   sa.ForeignKey("garment_type.id"), nullable=True))
    op.add_column("bom", sa.Column("dcm_base_size", sa.String(10), nullable=True))


def downgrade():
    op.drop_column("bom", "dcm_base_size")
    op.drop_column("bom", "garment_type_id")
    op.drop_column("bom", "cutting_confirmed_at")
    op.drop_column("bom", "cutting_confirmed_by")
    op.drop_column("bom_item", "dcm_confidence")
    op.drop_column("bom_item", "dcm_source")

    op.drop_index("ix_pattern_reference_pattern_code", table_name="pattern_reference")
    op.drop_index("ix_pattern_reference_spec_sheet_id", table_name="pattern_reference")
    op.drop_index("ix_pattern_reference_client_id", table_name="pattern_reference")
    op.drop_table("pattern_reference")

    op.drop_index("ix_style_consumption_signature", table_name="style_consumption_template")
    op.drop_index("ix_style_consumption_client_id", table_name="style_consumption_template")
    op.drop_table("style_consumption_template")

    op.drop_index("ix_pom_measurement_spec_sheet_id", table_name="pom_measurement")
    op.drop_table("pom_measurement")

    op.drop_index("ix_pom_dictionary_pom_code", table_name="pom_dictionary")
    op.drop_index("ix_pom_dictionary_source_term", table_name="pom_dictionary")
    op.drop_index("ix_pom_dictionary_language", table_name="pom_dictionary")
    op.drop_table("pom_dictionary")

    op.drop_index("ix_garment_type_code", table_name="garment_type")
    op.drop_table("garment_type")
