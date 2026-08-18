"""style.needs_lining — the DM's release-time lining declaration

Revision ID: 20260818_style_lining
Revises: 20260817_release_payroll
Create Date: 2026-08-18

WHAT THIS ADDS, AND WHY IT IS NULLABLE
──────────────────────────────────────────────────────────────────────────────

`style.needs_lining` stores the answer to the question the release screen now
asks outright: "does this style take a lining?". Until now that was INFERRED —
from the style's name (KNIT / WOOL / FUR …), from the SKU's lining colour
columns, and from a stored per-piece flag written once at upload. Inference
cannot be right about the exceptions, and the exceptions are the expensive ones:
a garment that walked to PACKAGE_EXPORT with no lining ever cut is the bug this
whole line of work started from.

BOOLEAN, NULLABLE — AND THE NULL IS THE POINT.
    Three states, because there are three real answers:

        NULL   nobody has been asked. Every style released before this column
               existed is in this state, and it falls back to the existing
               inference, so nothing already on the floor changes its verdict.
        TRUE   the DM declared it lined.
        FALSE  the DM declared it leather-only.

    NO SERVER DEFAULT, and no backfill. A NOT NULL column defaulting to FALSE
    would declare every historical style leather-only in one statement — which
    on the live database is roughly 1,400 pieces silently losing their lining
    leg, i.e. the original bug reintroduced by a migration. A default of TRUE is
    equally wrong in the other direction: it would strand every genuinely
    leather-only drawer at the completeness gate forever. The honest value for
    "we never asked" is NULL.

NO INDEX.
    It is read per-style on paths that have already selected the style row (the
    release screen, the store completeness gate, the piece-state read) and is
    never a search predicate on its own. An index here would be write cost for
    no read.

PORTABILITY
    Plain ADD COLUMN of a nullable BOOLEAN — no enum, no server_default, no
    Postgres-only syntax, so it runs identically on SQLite (where the test suite
    lives) and Postgres. Nothing to guard by dialect.
"""
from alembic import op
import sqlalchemy as sa

revision = "20260818_style_lining"
down_revision = "20260817_release_payroll"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "style",
        sa.Column("needs_lining", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("style", "needs_lining")
