"""drop employee.daily_rate — dead column

Revision ID: 20260717_1100_drop_daily_rate
Revises: 20260717_1000_style_code
Create Date: 2026-07-17

SEPARATE FROM THE STYLE-CODE MIGRATION ON PURPOSE.
  20260717_1000_style_code is REQUIRED — the wages rate API cannot address a style
  without it. This one is optional cleanup that DESTROYS DATA. Splitting them lets
  you `alembic upgrade 20260717_1000_style_code` and ship the rate screens while
  still thinking about this. Do not merge them.

WHY
  daily_rate is never read. Not by wages (a piece-rate wage is qty * rate, with no
  daily floor and no minimum guarantee — confirmed), not by attendance, not by
  anything. It is a column that looks authoritative and is not, which is exactly the
  kind of thing that gets wired into a payroll calculation by someone who assumes it
  means what it says.

  The attendance module's __init__ docstring still advertises "piece-rate floor
  hours", which is where the field's original intent lived. Fix that docstring in the
  same PR or the next reader re-adds the column.

BEFORE RUNNING
  A downgrade restores the column but NOT the values — there is nothing to restore
  them from. If any row has a non-null daily_rate you want to keep, dump it first:

      \\copy (SELECT id, name, daily_rate FROM employee WHERE daily_rate IS NOT NULL)
        TO 'daily_rate_backup.csv' CSV HEADER

  The upgrade below prints the count it is about to destroy. Read it.
"""
import sqlalchemy as sa
from alembic import op

revision = "20260717_1100_drop_daily_rate"
down_revision = "20260717_1000_style_code"
branch_labels = None
depends_on = None

def upgrade() -> None:
    bind = op.get_bind()
    n = bind.execute(
        sa.text("SELECT count(*) FROM employee WHERE daily_rate IS NOT NULL")
    ).scalar_one()
    if n:
        print(
            f"  [20260717_1100] dropping employee.daily_rate — {n} non-null value(s) "
            f"will be destroyed and are NOT recoverable by downgrade."
        )
    op.drop_column("employee", "daily_rate")


def downgrade() -> None:
    # Restores the column, not the data. The values are gone.
    op.add_column("employee", sa.Column("daily_rate", sa.Numeric(10, 2), nullable=True))
