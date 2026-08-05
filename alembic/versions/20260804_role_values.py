"""user_role: add 'security' and 'merchandiser'

Revision ID: 20260804_role_values
Revises: supplier_order_spec
Create Date: 2026-08-04

WHY BOTH VALUES

  security      UserRole.SECURITY has existed in the PYTHON enum for a while and
                is load-bearing today — it is in ATTENDANCE_OPERATOR_ROLES, so a
                gate operator's login depends on it. But it was never added to
                the native PG type: the baseline (20260613_1238) created
                user_role with 9 values and 20260730_barcode added only
                'lining_manager'. Every attempt to INSERT a security login on
                Postgres therefore fails with an invalid-enum-value error. This
                migration is what makes that role actually usable.

  merchandiser  New role for the client-facing order/sample coordinator.

app_user.role is Enum(UserRole, name="user_role") — a NATIVE PG type — so adding
a member to the Python enum is never enough on its own (CLAUDE.md §13).

F63 (same reasoning as 20260730_barcode): ALTER TYPE ... ADD VALUE cannot run
inside a transaction that then USES the new value, and PostgreSQL best practice
is to commit the enum addition on its own. autocommit_block() takes us out of
Alembic's migration transaction for these two idempotent statements. Committing
early is safe because ADD VALUE IF NOT EXISTS is re-runnable.

No-op on SQLite (the test stand-in), where there is no native enum type.
"""
from alembic import op

revision = "20260804_role_values"
down_revision = "supplier_order_spec"   # verify with `alembic heads` before running
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'security'")
            op.execute("ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'merchandiser'")


def downgrade() -> None:
    # PostgreSQL cannot remove a value from an enum type. Removing it would mean
    # recreating the type and rewriting every dependent column — far more
    # destructive than the extra value is worth. Deliberate no-op.
    pass
