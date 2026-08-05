"""stage gating, employee uniqueness, wage recompute

Revision ID: 20260724_01_july28_fixes
"""
from alembic import op
import sqlalchemy as sa

revision = "20260724_01_july28_fixes"
down_revision = "20260717_1100_drop_daily_rate"


def upgrade() -> None:
    # ── 1. Employee: uppercase designations, unique names ──────────────────
    op.execute("""
        UPDATE employee
        SET designation = UPPER(REGEXP_REPLACE(TRIM(designation), '[\\s\\-/]+', '_', 'g'))
        WHERE designation IS NOT NULL
    """)
    # Disambiguate pre-existing duplicate names BEFORE the unique index, or the
    # index creation fails on live data. Oldest row keeps the bare name.
    op.execute("""
        WITH ranked AS (
            SELECT id, name,
                   ROW_NUMBER() OVER (PARTITION BY LOWER(name) ORDER BY created_at, id) AS rn
            FROM employee
        )
        UPDATE employee e
        SET name = CASE WHEN r.rn = 2 THEN 'IN-CHAL ' || r.name
                        ELSE 'IN-CHAL-' || (r.rn - 1) || ' ' || r.name END
        FROM ranked r
        WHERE e.id = r.id AND r.rn > 1
    """)
    op.create_index("uq_employee_name_ci", "employee",
                    [sa.text("LOWER(name)")], unique=True)

    # ── 2. Operations: rename to canonical codes + add FINAL_INSPECTION ────
    for old, new in [("SHELL", "SHELL_STITCHING"), ("L/A", "LINE_ATTACH"),
                     ("LINING STICH", "LINE_STITCHING"), ("FF", "FINAL_FINISH")]:
        op.execute(sa.text(
            "UPDATE operation SET code = :new WHERE code = :old"
        ).bindparams(new=new, old=old))
    op.execute("""
        INSERT INTO operation (id, code, label, sequence, is_active, created_at, updated_at)
        SELECT gen_random_uuid(), 'FINAL_INSPECTION', 'Final Inspection', 8, TRUE, NOW(), NOW()
        WHERE NOT EXISTS (SELECT 1 FROM operation WHERE code = 'FINAL_INSPECTION')
    """)

    # ── 3. Wage run: recompute audit ──────────────────────────────────────
    op.add_column("wage_run", sa.Column("recompute_count", sa.Integer(),
                                        nullable=False, server_default="0"))
    op.add_column("wage_run", sa.Column("last_recomputed_at",
                                        sa.DateTime(timezone=True), nullable=True))
    op.add_column("wage_run", sa.Column("last_recomputed_by",
                                        sa.String(120), nullable=True))

    # ── 4. Frozen per-style/operation wage breakdown ──────────────────────
    op.create_table(
        "wage_line_detail",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("wage_run_id", sa.Uuid(),
                  sa.ForeignKey("wage_run.id", ondelete="CASCADE"), nullable=False),
        sa.Column("employee_id", sa.Uuid(),
                  sa.ForeignKey("employee.id"), nullable=False),
        sa.Column("style_id", sa.Uuid(), sa.ForeignKey("style.id"), nullable=False),
        sa.Column("operation_id", sa.Uuid(),
                  sa.ForeignKey("operation.id"), nullable=False),
        sa.Column("pieces", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rate", sa.Numeric(10, 2), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.UniqueConstraint("wage_run_id", "employee_id", "style_id", "operation_id",
                            name="uq_wage_line_detail"),
    )
    op.create_index("ix_wage_line_detail_run", "wage_line_detail", ["wage_run_id"])


def downgrade() -> None:
    op.drop_table("wage_line_detail")
    op.drop_column("wage_run", "last_recomputed_by")
    op.drop_column("wage_run", "last_recomputed_at")
    op.drop_column("wage_run", "recompute_count")
    op.drop_index("uq_employee_name_ci", table_name="employee")