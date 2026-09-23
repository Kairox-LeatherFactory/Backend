"""Add status/confidence to pom_dictionary

Revision ID: 20260911_pom_status
Revises: 20260911_order_style_fk
Create Date: 2026-09-11

UPDATED 2026-09-11 (Hamthan): service.py's LLM fallback for unresolved POM
terms (_resolve_and_persist_poms) already tried to persist a mapping with
status="suggested" plus a confidence score whenever the seeded YAML
dictionary had no entry for a term, mirroring the suggested/confirmed
pattern OrderStyle already uses for spec/DXF matching. Neither column
existed on pom_dictionary, so every such call crashed generate_bom_for_style
with TypeError instead of ever writing the row. server_default='confirmed'
marks every pre-existing (seeded + admin-added) row with its current,
already-trusted meaning; only the LLM path will ever write 'suggested' from
here on, so this global, shared dictionary doesn't quietly fill up with
unreviewed guesses indistinguishable from curated entries.
"""
from alembic import op
import sqlalchemy as sa

revision = "20260911_pom_status"
down_revision = "20260911_order_style_fk"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pom_dictionary",
        sa.Column("status", sa.String(20), nullable=False, server_default="confirmed"),
        if_not_exists=True,
    )
    op.add_column(
        "pom_dictionary",
        sa.Column("confidence", sa.Numeric(5, 4), nullable=True),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_column("pom_dictionary", "confidence")
    op.drop_column("pom_dictionary", "status")
