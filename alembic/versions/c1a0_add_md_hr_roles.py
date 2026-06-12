"""add MANAGING_DIRECTOR, HR (and SUPERVISOR) to user_role enum

Stage 0 — BOM Procurement Workflow. Decision 2: split MD vs DM, add HR.

ISOLATED ON PURPOSE (spec §3, "Enum value adds"):
    Postgres `ALTER TYPE ... ADD VALUE` is non-transactional and NOT reversible —
    there is no `DROP VALUE`. So this lives in its own migration with
    `downgrade = pass`. Removing a value would require recreating the type and
    rewriting every column that uses it; out of scope for Stage 0.

The live `user_role` enum was created with only the original six labels
(DIRECT_MANAGER, CUTTING_MANAGER, STITCHING_MANAGER, EMPLOYEE, CLIENT, VIEWER).
The ORM (`app/core/enums.py::UserRole`, a native `Enum(UserRole, name="user_role")`
keyed by member NAME) now also expects SUPERVISOR, MANAGING_DIRECTOR and HR, so all
three are added here. `IF NOT EXISTS` keeps it idempotent across environments whose
enum may already carry SUPERVISOR.

Revision ID: c1a0_add_md_hr_roles
Revises: 2443601a7f8a
Create Date: 2026-06-11
"""
from alembic import op

revision = "c1a0_add_md_hr_roles"
down_revision = "2443601a7f8a"
branch_labels = None
depends_on = None

# DB labels are the enum MEMBER NAMES (SQLAlchemy default for Enum(PyEnum)).
_NEW_ROLES = ("SUPERVISOR", "MANAGING_DIRECTOR", "HR")


def upgrade():
    for label in _NEW_ROLES:
        op.execute(f"ALTER TYPE user_role ADD VALUE IF NOT EXISTS '{label}'")


def downgrade():
    # Irreversible: Postgres has no ALTER TYPE ... DROP VALUE. Leaving the labels
    # in place is harmless (no row references them after a downgrade of the role
    # logic). See module docstring.
    pass
