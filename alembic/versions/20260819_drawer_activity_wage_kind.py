"""Drawer activity clock + wage run kind, scope label and frozen unrated warning.

Three independent additions, one migration because they ship together:

DRAWER — `last_activity_at` / `last_activity_kind`
    The store screen orders by "most recently worked on". It used to derive that
    from coalesce(sended_at, received_at, created_at), which could not see three
    of the five events that matter: a MERGE and a STORE SCAN write neither
    column, and RELEASE nulls both — so the drawer that had just shipped sorted
    to the bottom, and a 200-drawer pool bootstrapped in one transaction shares
    one created_at and ranked arbitrarily forever.

    BACKFILLED FROM `updated_at`, not left NULL. TimestampMixin sets
    onupdate=func.now(), so every drawer the ORM has ever touched already carries
    a truthful activity time; copying it means the new ordering is correct on
    day one instead of after every drawer has been handled once. The read path
    still coalesces through updated_at, so a NULL here is harmless.

WAGE_RUN — `run_kind` / `scope_is_label`
    Payroll splits into PIECE (must name an order/style) and MONTHLY (no paying
    scope; may carry an order/style purely as a screen label). Existing rows paid
    BOTH populations from one window, so they backfill to 'combined' — the value
    the overlap guard treats as colliding with everything, which is exactly what
    those runs really did.

    STORED AS VARCHAR, NOT A NATIVE PG ENUM. See CLAUDE.md §13: this schema has
    been bitten three times by an enum label that had to be ALTER TYPE-d in
    before any INSERT could name it (lining_manager, security, merchandiser). A
    VARCHAR needs no type surgery and behaves identically on SQLite.

WAGE_RUN — `unrated_snapshot`
    The "these pieces went unpaid because their style/operation had no rate"
    warning existed only in the HTTP response of the compute call; every later
    read hardcoded []. Frozen onto the run so re-reading a payslip cannot
    silently lose it. Existing rows get NULL, which reads as "not recorded" —
    honest, and distinct from a stored empty list meaning "nothing was unrated".

Revision ID: 20260819_drw_wage
Revises: 20260818_fk_setnull
"""
from alembic import op
import sqlalchemy as sa


revision = "20260819_drw_wage"
down_revision = "20260818_fk_setnull"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"

    # ── drawer ───────────────────────────────────────────────────────────────
    op.add_column("drawer", sa.Column(
        "last_activity_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("drawer", sa.Column(
        "last_activity_kind", sa.String(length=20), nullable=True))
    op.create_index("ix_drawer_last_activity_at", "drawer", ["last_activity_at"])

    # Seed the clock so "recent" is meaningful immediately. updated_at is never
    # NULL (server_default=now()), so this leaves no gaps.
    op.execute("UPDATE drawer SET last_activity_at = updated_at "
               "WHERE last_activity_at IS NULL")

    # ── wage_run ─────────────────────────────────────────────────────────────
    op.add_column("wage_run", sa.Column(
        "run_kind", sa.String(length=20), nullable=False,
        server_default="combined"))
    op.create_index("ix_wage_run_run_kind", "wage_run", ["run_kind"])
    op.add_column("wage_run", sa.Column(
        "scope_is_label", sa.Boolean(), nullable=False,
        server_default=sa.text("0" if is_sqlite else "false")))
    op.add_column("wage_run", sa.Column(
        "unrated_snapshot", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("wage_run", "unrated_snapshot")
    op.drop_column("wage_run", "scope_is_label")
    op.drop_index("ix_wage_run_run_kind", table_name="wage_run")
    op.drop_column("wage_run", "run_kind")

    op.drop_index("ix_drawer_last_activity_at", table_name="drawer")
    op.drop_column("drawer", "last_activity_kind")
    op.drop_column("drawer", "last_activity_at")
