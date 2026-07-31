"""B5: one wage line per employee per run.

Revision ID: 20260731_wage_line_uniq
Revises: 20260730_barcode
"""
import sqlalchemy as sa
from alembic import op

revision = "20260731_wage_line_uniq"
down_revision = "20260730_barcode"      # verify with `alembic heads`
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Collapse any pre-existing duplicates first, keeping the largest line —
    # a duplicate here means someone was already paid twice; the survivor is
    # the one that matches the payslip that was printed.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("""
            DELETE FROM wage_line a
            USING wage_line b
            WHERE a.wage_run_id = b.wage_run_id
              AND a.employee_id = b.employee_id
              AND (a.amount < b.amount
                   OR (a.amount = b.amount AND a.id < b.id))
        """)
    op.create_unique_constraint(
        "uq_wage_line_run_emp", "wage_line", ["wage_run_id", "employee_id"])


def downgrade() -> None:
    op.drop_constraint("uq_wage_line_run_emp", "wage_line", type_="unique")