"""Breakdown release gate + payroll scope/reopen audit

Revision ID: 20260817_release_payroll
Revises: 20260813_drop_drawer_sent_to
Create Date: 2026-08-17

WHAT THIS ADDS, AND WHY EACH COLUMN EXISTS
──────────────────────────────────────────────────────────────────────────────

1. style.production_status / released_at / released_by      (change-list item 9)

   The breakdown release gate. Uploading a sheet no longer mints per-piece
   barcodes and drawer merges; the DM releases the styles that actually go to
   production, and only then does the mint happen.

   VARCHAR, NOT A NATIVE PG ENUM — deliberately.
       `app_user.role` is a native enum, and adding a label to it has broken a
       deploy three separate times (20260804, 20260810_role_case, and the
       STORE_MANAGER add in 20260813). The trap is that
       `Enum(PyEnum, name=...)` persists the MEMBER NAME, so a hand-written
       migration that adds the lowercase VALUE creates a label the ORM will
       never emit and the INSERT still fails.

       Every module-level status in this schema (attendance_source aside) is a
       str-Enum over VARCHAR for exactly that reason. Adding a fourth release
       state must never need an ALTER TYPE, and with VARCHAR it does not.

   BACKFILL: every EXISTING style is stamped RELEASED, not DRAFT. Those styles
   already have minted pieces with printed, scanned barcodes on the floor —
   calling them DRAFT would tell the release screen they are unminted and invite
   a second mint over live garments. The server_default is DRAFT so everything
   uploaded from now on lands unreleased.

2. wage_run.scope_order_number / scope_style_code            (change-list item 3)

   A payroll run may be narrowed to one order or one style. Stored on the run
   (rather than passed again at recompute time) so a recompute reproduces the
   SAME scope — a recompute that silently widened a one-style run into a
   whole-factory one would pay everyone twice for the window.

   Stored as the human CODE, not an id: a run reprinted after a style is renamed
   must still say which style it paid for, and the code is what appears on the
   printed traveler.

3. wage_run.reopen_count / last_reopened_at / _by / _reason  (change-list item 3)

   The resolution of the "recompute the frozen payroll" guardrail conflict. A
   CLOSED run is frozen; the only way to change it is an explicit, audited
   REOPEN that captures a REASON. Counted separately from recompute_count
   because "recomputed twice" and "unfrozen twice after payment" are different
   facts about a payslip, and only the second one needs explaining.

NO ENUM TYPES ARE TOUCHED, so this migration is fully transactional — no
autocommit_block, nothing to un-wedge if it fails halfway. That is a property
worth keeping: check it before adding anything to this file.

Portability watch (CLAUDE.md §13): no JSONB, no ON CONFLICT, no
gen_random_uuid(). Runs identically on SQLite for the test suite.
"""
import sqlalchemy as sa
from alembic import op

revision = "20260817_release_payroll"
down_revision = "20260813_drop_sent_to"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. the breakdown release gate ────────────────────────────────────────
    op.add_column(
        "style",
        sa.Column("production_status", sa.String(length=20), nullable=False,
                  server_default="DRAFT"),
    )
    op.add_column("style",
                  sa.Column("released_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("style",
                  sa.Column("released_by", sa.String(length=120), nullable=True))
    op.create_index("ix_style_production_status", "style", ["production_status"])

    # EXISTING STYLES ARE RELEASED, NOT DRAFT. They already have minted pieces
    # whose barcodes are printed and being scanned; showing them as DRAFT would
    # offer the DM a "release" button over live garments. Keyed on the pieces
    # actually existing rather than on "every row", so a style uploaded but never
    # minted (there should be none, but the loader has been changed twice) stays
    # DRAFT and can be released properly.
    op.execute("""
        UPDATE style SET production_status = 'RELEASED'
        WHERE id IN (
            SELECT DISTINCT sku.style_id
            FROM sku JOIN piece ON piece.sku_id = sku.id
        )
    """)

    # ── 2. payroll run scope ─────────────────────────────────────────────────
    op.add_column("wage_run",
                  sa.Column("scope_order_number", sa.String(length=50), nullable=True))
    op.add_column("wage_run",
                  sa.Column("scope_style_code", sa.String(length=200), nullable=True))
    op.create_index("ix_wage_run_scope_order_number", "wage_run",
                    ["scope_order_number"])
    op.create_index("ix_wage_run_scope_style_code", "wage_run",
                    ["scope_style_code"])

    # ── 3. payroll reopen audit ──────────────────────────────────────────────
    op.add_column(
        "wage_run",
        sa.Column("reopen_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("wage_run",
                  sa.Column("last_reopened_at", sa.DateTime(timezone=True),
                            nullable=True))
    op.add_column("wage_run",
                  sa.Column("last_reopened_by", sa.String(length=120), nullable=True))
    op.add_column("wage_run",
                  sa.Column("last_reopen_reason", sa.String(length=500),
                            nullable=True))


def downgrade() -> None:
    op.drop_column("wage_run", "last_reopen_reason")
    op.drop_column("wage_run", "last_reopened_by")
    op.drop_column("wage_run", "last_reopened_at")
    op.drop_column("wage_run", "reopen_count")
    op.drop_index("ix_wage_run_scope_style_code", table_name="wage_run")
    op.drop_index("ix_wage_run_scope_order_number", table_name="wage_run")
    op.drop_column("wage_run", "scope_style_code")
    op.drop_column("wage_run", "scope_order_number")
    op.drop_index("ix_style_production_status", table_name="style")
    op.drop_column("style", "released_by")
    op.drop_column("style", "released_at")
    op.drop_column("style", "production_status")
