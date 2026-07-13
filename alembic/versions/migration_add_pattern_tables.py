"""add DXF pattern tables (pattern_extraction, pattern_piece, dxf_yield_observation)

Revision ID: b7d4f1a9c2e0
Revises: <your current head>
Create Date: 2026-06-29

These ARE real new tables (unlike the discarded QUEUED enum migration — that was a
no-op because SubmissionStatus is a VARCHAR). Run `alembic revision --autogenerate`
to confirm against your metadata, or apply this hand-written version. UUID() renders
as native UUID on Postgres and CHAR(32) on SQLite, matching the rest of the schema.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from sqlalchemy.dialects.postgresql import UUID
from app.core.models import JSON_VARIANT 

revision = "b7d4f1a9c2e0"
down_revision = "f3a9c1d2e8b7"  # <-- set to your current head
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pattern_extraction",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("client_id", UUID(), sa.ForeignKey("client.id"), nullable=True),
        sa.Column("style_signature", sa.String(120), nullable=False),
        sa.Column("garment_type_id", UUID(), sa.ForeignKey("garment_type.id"), nullable=True),
        sa.Column("source_document_id", UUID(), sa.ForeignKey("document.id"), nullable=True),
        sa.Column("source_system", sa.String(20)),
        sa.Column("parser_version", sa.String(40)),
        sa.Column("unit", sa.String(4)),
        sa.Column("master_size", sa.String(10)),
        sa.Column("n_pieces", sa.Integer()),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(300)),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("area_matrix", JSON_VARIANT()),
        sa.Column("fabric_matrix", JSON_VARIANT()),
        sa.Column("fabric_roles", JSON_VARIANT()),
        sa.Column("warnings", JSON_VARIANT()),
        sa.UniqueConstraint("style_signature", "sha256", name="uq_pattern_extraction_file"),
    )
    op.create_index("ix_pattern_extraction_client_id", "pattern_extraction", ["client_id"])
    op.create_index("ix_pattern_extraction_style_signature", "pattern_extraction", ["style_signature"])
    op.create_index("ix_pattern_extraction_sha256", "pattern_extraction", ["sha256"])
    op.create_index("ix_pattern_extraction_is_current", "pattern_extraction", ["is_current"])

    op.create_table(
        "pattern_piece",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("pattern_extraction_id", UUID(),
                  sa.ForeignKey("pattern_extraction.id"), nullable=False),
        sa.Column("block", sa.String(120)),
        sa.Column("name", sa.String(200)),
        sa.Column("fabric", sa.String(120)),
        sa.Column("size", sa.String(10)),
        sa.Column("qty", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("net_area_sf", sa.Numeric(10, 3)),
        sa.Column("longest_cm", sa.Numeric(8, 1)),
    )
    op.create_index("ix_pattern_piece_pattern_extraction_id", "pattern_piece",
                    ["pattern_extraction_id"])

    op.create_table(
        "dxf_yield_observation",
        sa.Column("id", UUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("style_signature", sa.String(120), nullable=False),
        sa.Column("species", sa.String(20), nullable=False),
        sa.Column("size", sa.String(10)),
        sa.Column("net_qty_sf", sa.Numeric(10, 3), nullable=False),
        sa.Column("confirmed_dcm_sf", sa.Numeric(12, 3), nullable=False),
        sa.Column("implied_yield", sa.Numeric(6, 3), nullable=False),
        sa.Column("source_bom_id", UUID(), sa.ForeignKey("bom.id"), nullable=True),
        sa.Column("confirmed_by", UUID(), sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_dxf_yield_observation_style_signature", "dxf_yield_observation",
                    ["style_signature"])
    op.create_index("ix_dxf_yield_observation_species", "dxf_yield_observation", ["species"])


def downgrade() -> None:
    op.drop_table("dxf_yield_observation")
    op.drop_table("pattern_piece")
    op.drop_table("pattern_extraction")
