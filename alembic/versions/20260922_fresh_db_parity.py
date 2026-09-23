"""Close the last gaps between a migrations-only database and models.py.

Found by building a brand-new Postgres purely with `alembic upgrade head` and
diffing it against Base.metadata (alembic.autogenerate.compare_metadata). The
live databases never showed these gaps because DEBUG `create_all` had already
built the objects from the models:

  * six model indexes no revision created
  * the old ix_wage_line_detail_run index, superseded by ix_wage_line_detail_wage_run_id
  * dxf_yield.created_at and supplier_order.created_at/updated_at NOT NULL
  * production_event.employee_id FK missing ON DELETE SET NULL. 20260818_fk_setnull
    only covered columns that were nullable at the time, and employee_id became
    nullable later, in 20260905_job_work.

Every step is idempotent, so it is a no-op on databases that already match.

Revision ID: 20260922_fresh_db_parity
Revises: 20260917_bom_per_style
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260922_fresh_db_parity"
down_revision = "20260917_bom_per_style"
branch_labels = None
depends_on = None

_INDEXES = [
    ("ix_production_event_leather_lot_id", "production_event", "leather_lot_id"),
    ("ix_production_event_lining_lot_id", "production_event", "lining_lot_id"),
    ("ix_style_material_spec_style_id", "style_material_spec", "style_id"),
    ("ix_supplier_order_supplier_id", "supplier_order", "supplier_id"),
    ("ix_wage_line_detail_employee_id", "wage_line_detail", "employee_id"),
    ("ix_wage_line_detail_wage_run_id", "wage_line_detail", "wage_run_id"),
]
_NOT_NULL = [
    ("dxf_yield", "created_at"),
    ("supplier_order", "created_at"),
    ("supplier_order", "updated_at"),
]


def upgrade() -> None:
    for name, table, col in _INDEXES:
        op.create_index(name, table, [col], if_not_exists=True)
    op.drop_index("ix_wage_line_detail_run", table_name="wage_line_detail",
                  if_exists=True)

    for table, col in _NOT_NULL:
        op.execute(f"UPDATE {table} SET {col} = now() WHERE {col} IS NULL")
        op.alter_column(table, col, nullable=False,
                        existing_type=postgresql.TIMESTAMP(timezone=True))

    op.execute("ALTER TABLE production_event DROP CONSTRAINT IF EXISTS "
               "fk_production_event_employee_id_employee")
    op.create_foreign_key("fk_production_event_employee_id_employee",
                          "production_event", "employee",
                          ["employee_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE production_event DROP CONSTRAINT IF EXISTS "
               "fk_production_event_employee_id_employee")
    op.create_foreign_key("fk_production_event_employee_id_employee",
                          "production_event", "employee",
                          ["employee_id"], ["id"])
    for table, col in _NOT_NULL:
        op.alter_column(table, col, nullable=True,
                        existing_type=postgresql.TIMESTAMP(timezone=True))
    op.create_index("ix_wage_line_detail_run", "wage_line_detail",
                    ["wage_run_id"], if_not_exists=True)
    for name, table, _ in _INDEXES:
        op.drop_index(name, table_name=table, if_exists=True)
