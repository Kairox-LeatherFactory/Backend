"""add attribution axis to bom_item + workflow columns to fabric_role

Revision ID: add_attribution_axis
Revises: <SET_TO_CURRENT_HEAD>          # TODO: `alembic heads` — set before running.
Create Date: 2026-07-09

NOTE (per your standing preference, Alembic is applied last): this migration is
additive and safe to batch with the config-to-DB work. BEFORE running ANY new
migration, resolve the two-root problem first — migration b7d4f1a9c2e0 has
down_revision=None, creating a second root; `alembic upgrade head` will fail
with "multiple heads" until that is chained. Set `down_revision` below to the
real current head once the roots are merged.

Two SEPARATE axes on bom_item (do not conflate):
  dcm_source / dcm_confidence         -> yield axis (already present; unchanged)
  attribution_* (added here)          -> label→material mapping axis
"""
from alembic import op
import sqlalchemy as sa

from app.core.models import GUID

revision = "fiber_role_bom_item"
down_revision = "add_material_rate"          # TODO: set to current head (see note above)
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── bom_item: the attribution axis ──────────────────────────────────────
    op.add_column("bom_item", sa.Column("attribution_source", sa.String(20), nullable=True))
    op.add_column("bom_item", sa.Column("attribution_confidence", sa.Numeric(5, 4), nullable=True))
    # Existing rows were lexicon/template/manual resolved => treat as confirmed so
    # they are NOT retroactively flagged. New AI lines set 'estimated' explicitly.
    op.add_column("bom_item", sa.Column("attribution_status", sa.String(20),
                                        nullable=False, server_default="confirmed"))

    # ── fabric_role: the suggest→confirm→persist workflow ───────────────────
    # Seeded rows are authoritative => default 'confirmed'; AI proposals write 'suggested'.
    op.add_column("fabric_role", sa.Column("status", sa.String(20),
                                           nullable=False, server_default="confirmed"))
    op.add_column("fabric_role", sa.Column("confidence", sa.Numeric(5, 4), nullable=True))
    op.add_column("fabric_role", sa.Column("source", sa.String(20),
                                           nullable=True, server_default="seed"))
    op.add_column("fabric_role", sa.Column("suggested_by", GUID(), nullable=True))
    op.add_column("fabric_role", sa.Column("confirmed_by", GUID(), nullable=True))
    op.create_index("ix_fabric_role_status", "fabric_role", ["status"])


def downgrade() -> None:
    op.drop_index("ix_fabric_role_status", table_name="fabric_role")
    for col in ("confirmed_by", "suggested_by", "source", "confidence", "status"):
        op.drop_column("fabric_role", col)
    for col in ("attribution_status", "attribution_confidence", "attribution_source"):
        op.drop_column("bom_item", col)