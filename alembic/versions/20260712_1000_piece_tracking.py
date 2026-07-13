"""per-piece tracking + sku.code slug: piece table, production_event.piece_id, sku.code

Revision ID: 20260712_1000_piece_tracking
Revises: fiber_role_bom_item
Create Date: 2026-07-12

Three coherent changes:
  1. sku.code — a deterministic, globally-unique, readable slug
     {ORDER}-{STYLE}-{COLOUR}-{SIZE} (article excluded), backfilled for existing
     rows to EXACTLY match make_sku_code() in clients.service, then given a
     unique index. Users pick this in the UI; sku_id stays internal.
  2. piece — per-piece table; identity (sku_id, seq); code is the printed id
     ({sku.code}-{seq}). NO unique(piece_id, operation_id): rework is permitted.
  3. production_event — nullable piece_id (legacy rows stay valid), and DROP
     bundle_ref + its index (the piece is the tracked unit; no bundle_ref).

Runs on Postgres. The SQLite test harness builds from models via create_all.
NOTE (per-date lines): this migration keeps SKU one-per-triple. The separate
sku_order_line table for per-date quantities is a FOLLOW-UP migration, not here.
"""
import re

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "20260712_1000_piece_tracking"
down_revision = "fiber_role_bom_item"
branch_labels = None
depends_on = None


def _slug(s) -> str:
    out = re.sub(r"[^A-Za-z0-9]+", "_", (str(s) if s is not None else "").strip()).strip("_").upper()
    return out or "NA"


def upgrade() -> None:
    # 1) sku.code -----------------------------------------------------------
    op.add_column("sku", sa.Column("code", sa.String(length=120), nullable=True))
    bind = op.get_bind()
    rows = bind.execute(sa.text(
        """
        SELECT s.id AS id, co.order_number AS order_number, st.name AS style_name,
               s.color_code AS color_code, s.color_name AS color_name, s.size AS size
        FROM sku s
        JOIN style st ON st.id = s.style_id
        JOIN client_order co ON co.id = st.client_order_id
        WHERE s.code IS NULL
        """
    )).mappings().all()
    for r in rows:
        code = "-".join((
            _slug(r["order_number"]), _slug(r["style_name"]),
            _slug(r["color_name"] or r["color_code"]), _slug(r["size"]),
        ))
        bind.execute(sa.text("UPDATE sku SET code = :c WHERE id = :i"),
                     {"c": code, "i": r["id"]})
    # Unique: a slug collision (e.g. two colours differing only by punctuation)
    # will FAIL LOUDLY here rather than silently duplicate — fix the data and re-run.
    op.create_index(op.f("ix_sku_code"), "sku", ["code"], unique=True)

    # 2) piece --------------------------------------------------------------
    op.create_table(
        "piece",
        sa.Column("code", sa.String(length=100), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("sku_id", UUID(), nullable=False),
        sa.Column("current_operation_id", UUID(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("id", UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["sku_id"], ["sku.id"],
                                name=op.f("fk_piece_sku_id_sku")),
        sa.ForeignKeyConstraint(["current_operation_id"], ["operation.id"],
                                name=op.f("fk_piece_current_operation_id_operation")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_piece")),
        sa.UniqueConstraint("sku_id", "seq", name="uq_piece_sku_seq"),
    )
    op.create_index(op.f("ix_piece_code"), "piece", ["code"], unique=True)
    op.create_index(op.f("ix_piece_sku_id"), "piece", ["sku_id"], unique=False)

    # 3) production_event: +piece_id, -bundle_ref ---------------------------
    op.add_column("production_event", sa.Column("piece_id", UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_production_event_piece_id_piece"),
        "production_event", "piece", ["piece_id"], ["id"],
    )
    op.create_index(op.f("ix_production_event_piece_id"), "production_event",
                    ["piece_id"], unique=False)
    op.drop_index(op.f("ix_production_event_bundle_ref"), table_name="production_event")
    op.drop_column("production_event", "bundle_ref")


def downgrade() -> None:
    op.add_column("production_event",
                  sa.Column("bundle_ref", sa.String(length=60), nullable=True))
    op.create_index(op.f("ix_production_event_bundle_ref"), "production_event",
                    ["bundle_ref"], unique=False)
    op.drop_index(op.f("ix_production_event_piece_id"), table_name="production_event")
    op.drop_constraint(op.f("fk_production_event_piece_id_piece"),
                       "production_event", type_="foreignkey")
    op.drop_column("production_event", "piece_id")

    op.drop_index(op.f("ix_piece_sku_id"), table_name="piece")
    op.drop_index(op.f("ix_piece_code"), table_name="piece")
    op.drop_table("piece")

    op.drop_index(op.f("ix_sku_code"), table_name="sku")
    op.drop_column("sku", "code")