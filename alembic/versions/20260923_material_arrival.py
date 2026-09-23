"""Staged material arrivals: a delivery entered in two sittings.

The van turns up and whoever signs for it knows three things — article, colour,
total. The approved/rejected split and the per-hide measurements are twenty
minutes of quiet work that happens later. These columns are what let a receipt
be half-entered and then finished, instead of the floor either recording nothing
or inventing numbers to get past required fields.

EVERY EXISTING ROW READS COMPLETED, via the server default, and that is exactly
what it was: a delivery entered in one sitting. Nothing already in the building
changes meaning.

Revision ID: 20260923_material_arrival
Revises: 20260922_fresh_db_parity
"""
from alembic import op
import sqlalchemy as sa

from app.core.models import GUID

revision = "20260923_material_arrival"
down_revision = "20260922_fresh_db_parity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PENDING | COMPLETED. A plain String, not a native PG enum: the schema's
    # native enums are a migration hazard (CLAUDE.md §13 — you cannot drop a
    # label, and the ORM persists the member NAME), and this value set is owned
    # by one module and read by one query filter.
    op.add_column(
        "material_receipt",
        sa.Column("status", sa.String(20), nullable=False,
                  server_default="COMPLETED"),
    )
    # What was SAID to have arrived, kept beside what was later approved. The
    # completion corrects on_hand by (approved − declared); without the declared
    # figure it would have to assign instead, which would silently undo every cut
    # logged against the provisional stock in between.
    op.add_column("material_receipt",
                  sa.Column("declared_qty", sa.Numeric(14, 3), nullable=True))
    op.add_column("material_receipt",
                  sa.Column("declared_sheet_count", sa.Integer(), nullable=True))
    op.add_column("material_receipt",
                  sa.Column("completed_at", sa.DateTime(timezone=True),
                            nullable=True))
    # GUID() rather than postgresql.UUID: ids come from the app (uuid4) and this
    # schema degrades to CHAR(32) off Postgres, which is what keeps the SQLite
    # test harness a faithful stand-in (CLAUDE.md §13/§14).
    op.add_column("material_receipt",
                  sa.Column("completed_by", GUID(), nullable=True))
    op.add_column("material_receipt",
                  sa.Column("note", sa.String(300), nullable=True))

    op.create_foreign_key(
        "fk_material_receipt_completed_by_app_user",
        "material_receipt", "app_user", ["completed_by"], ["id"],
        ondelete="SET NULL",
    )
    # The worklist's only query is "what is still PENDING", and it has to stay
    # cheap as receipts accumulate — every delivery ever made lives in this table.
    op.create_index("ix_material_receipt_status", "material_receipt", ["status"])


def downgrade() -> None:
    op.drop_index("ix_material_receipt_status", table_name="material_receipt")
    op.drop_constraint("fk_material_receipt_completed_by_app_user",
                       "material_receipt", type_="foreignkey")
    for column in ("note", "completed_by", "completed_at",
                   "declared_sheet_count", "declared_qty", "status"):
        op.drop_column("material_receipt", column)
