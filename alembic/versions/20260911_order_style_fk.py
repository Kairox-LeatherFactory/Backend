"""Repoint order_style.pattern_reference_id at pattern_extraction, not pattern_reference

Revision ID: 20260911_order_style_fk
Revises: 20260908_material_lot_used
Create Date: 2026-09-11

UPDATED 2026-09-11 (Hamthan): order_style.pattern_reference_id backs the "DXF
match" feature (dxf_match_status, repo.patterns_for_client,
confirm_style_attachments) — it's meant to point at the DXF-parsed pattern
data in PatternExtraction (table pattern_extraction, populated by
POST /patterns -> ingest_pattern_dxf). It was wired instead to the unrelated
legacy PatternReference table (a free-text "follow pattern code X" concept
that nothing currently writes to), so POST /order-styles/{id}/attachments
with the pattern_id POST /patterns actually hands back died with
ForeignKeyViolationError — that id only ever exists in pattern_extraction.
See the matching comment on OrderStyle.pattern_reference_id in models.py.

This only repoints the constraint; the column and its data (existing
pattern_reference_id values, if any were ever successfully set against the
old table) are untouched. Any pre-existing pattern_reference_id values that
happen to not exist in pattern_extraction would violate the new FK — none
are expected since the old FK meant nothing could ever be attached through
the normal DXF-upload flow without already hitting the very error being
fixed here.

NOTE: this file was originally named/id'd
20260911_order_style_pattern_fk_fix, which is 36 characters — longer than
alembic_version.version_num's VARCHAR(32), so `alembic upgrade head` ran the
DDL, then failed writing the bookkeeping row and rolled the whole
transaction back (verified live: the constraint was still the pre-fix one
afterward, so nothing was left half-applied). Shortened the id here and
renamed the file to match.
"""
from alembic import op

revision = "20260911_order_style_fk"
down_revision = "20260908_material_lot_used"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "fk_order_style_pattern_reference_id_pattern_reference",
        "order_style", type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_order_style_pattern_reference_id_pattern_extraction",
        "order_style", "pattern_extraction",
        ["pattern_reference_id"], ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_order_style_pattern_reference_id_pattern_extraction",
        "order_style", type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_order_style_pattern_reference_id_pattern_reference",
        "order_style", "pattern_reference",
        ["pattern_reference_id"], ["id"],
        ondelete="SET NULL",
    )
