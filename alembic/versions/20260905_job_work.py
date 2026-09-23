"""work sent outside the factory, and what each piece costs

Revision ID: 20260905_job_work
Revises: 20260904_inspection
Create Date: 2026-09-05

THE GAP
──────────────────────────────────────────────────────────────────────────────
When a deadline is short, tailoring (or cutting, or lining) goes to an outside
factory. Nothing modelled that: the pieces stopped moving, nobody could say
where they were, and the money paid for the work was recorded nowhere. Every
stage in ProductionStage assumed in-house.

THREE TABLES
    vendor          the outside factory. Thin on purpose — the thing that varies
                    is the RATE, and the same tailor charges differently for a
                    bomber and a vest, so a rate on the vendor would be wrong the
                    first time it was used.
    job_work        one dispatch: these pieces, this stage, out on this date,
                    back by that one. `rate_per_piece` is OPTIONAL.
    job_work_piece  which garments went and which came back. A dispatch of forty
                    rarely returns as forty.

TWO CHANGES TO production_event, AND THE FIRST ONE NEEDS EXPLAINING
──────────────────────────────────────────────────────────────────────────────
    vendor_id    NEW. Set instead of employee_id when an outside factory did the
                 work.
    employee_id  BECOMES NULLABLE. A stage performed by a vendor has no
                 employee, and forcing a name there would credit one of our
                 workers with somebody else's work AND generate a wage line for
                 it. The wage query inner-joins Employee, so a NULL drops out of
                 payroll on its own — which is exactly right: nobody on our books
                 earned it.

                 Nothing existing is affected: every event written so far has an
                 employee, and the column stays indexed and foreign-keyed.

WHAT THE RATE BUYS
    cost = rate_per_piece x pieces that CAME BACK, derived from job_work_piece
    rather than stored — so a garment that never returned is never paid for, and
    a rejected one is a quality conversation rather than an invoice line.

PORTABILITY (CLAUDE.md §13): statuses are VARCHAR, not native enums. No JSONB,
no ON CONFLICT, ids from uuid4, constraints named to match NAMING_CONVENTION.
SQLite needs batch_alter_table to change a column's nullability, which is why
the employee_id alter is wrapped.
"""
from alembic import op
import sqlalchemy as sa

from sqlalchemy.dialects.postgresql import UUID as GUID

revision = "20260905_job_work"
down_revision = "20260904_inspection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"
    true_ = sa.text("1" if is_sqlite else "true")

    op.create_table(
        "vendor",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("contact", sa.String(length=200), nullable=True),
        sa.Column("note", sa.String(length=300), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False,
                  server_default=true_),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_vendor"),
        sa.UniqueConstraint("name", name="uq_vendor_name"),
    )
    op.create_index("ix_vendor_name", "vendor", ["name"])

    op.create_table(
        "job_work",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("vendor_id", GUID(), nullable=True),
        # A ProductionStage value: the work is the same work, simply done
        # elsewhere. Recording it as a different kind of thing would take those
        # garments out of every existing progress count.
        sa.Column("stage", sa.String(length=30), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expected_back", sa.Date(), nullable=True),
        sa.Column("returned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False,
                  server_default="OUT"),
        # OPTIONAL — some work is quoted per piece and some is settled another
        # way. Demanding a number nobody has yet means the dispatch does not get
        # recorded at all, which is the state this replaces.
        sa.Column("rate_per_piece", sa.Numeric(12, 2), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("dispatched_by", GUID(), nullable=True),
        sa.Column("received_by", GUID(), nullable=True),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_job_work"),
        sa.ForeignKeyConstraint(["vendor_id"], ["vendor.id"],
                                ondelete="SET NULL",
                                name="fk_job_work_vendor_id_vendor"),
        sa.ForeignKeyConstraint(["dispatched_by"], ["app_user.id"],
                                ondelete="SET NULL",
                                name="fk_job_work_dispatched_by_app_user"),
        sa.ForeignKeyConstraint(["received_by"], ["app_user.id"],
                                ondelete="SET NULL",
                                name="fk_job_work_received_by_app_user"),
    )
    op.create_index("ix_job_work_vendor_id", "job_work", ["vendor_id"])
    op.create_index("ix_job_work_stage", "job_work", ["stage"])
    op.create_index("ix_job_work_status", "job_work", ["status"])
    op.create_index("ix_job_work_expected_back", "job_work", ["expected_back"])
    op.create_index("ix_job_work_vendor_status", "job_work",
                    ["vendor_id", "status"])

    op.create_table(
        "job_work_piece",
        sa.Column("id", GUID(), nullable=False),
        # NOT NULL + CASCADE — a dispatch line is OWNED by its dispatch, not
        # merely referencing it. Nullable would need SET NULL, leaving orphan
        # lines claiming pieces are out at a job that no longer exists.
        sa.Column("job_work_id", GUID(), nullable=False),
        sa.Column("piece_id", GUID(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False,
                  server_default="OUT"),
        sa.Column("returned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.String(length=300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_job_work_piece"),
        sa.UniqueConstraint("job_work_id", "piece_id",
                            name="uq_job_work_piece"),
        sa.ForeignKeyConstraint(["job_work_id"], ["job_work.id"],
                                ondelete="CASCADE",
                                name="fk_job_work_piece_job_work_id_job_work"),
        sa.ForeignKeyConstraint(["piece_id"], ["piece.id"], ondelete="SET NULL",
                                name="fk_job_work_piece_piece_id_piece"),
    )
    op.create_index("ix_job_work_piece_job_work_id", "job_work_piece",
                    ["job_work_id"])
    op.create_index("ix_job_work_piece_piece_id", "job_work_piece", ["piece_id"])
    op.create_index("ix_job_work_piece_status", "job_work_piece", ["status"])
    op.create_index("ix_job_work_piece_piece_status", "job_work_piece",
                    ["piece_id", "status"])

    # ── production_event learns about vendors ────────────────────────────────
    op.add_column("production_event", sa.Column("vendor_id", GUID(),
                                                nullable=True))
    op.create_index("ix_production_event_vendor_id", "production_event",
                    ["vendor_id"])
    op.create_foreign_key("fk_production_event_vendor_id_vendor",
                          "production_event", "vendor",
                          ["vendor_id"], ["id"], ondelete="SET NULL")

    # employee_id becomes NULLABLE: a vendor-performed stage has no employee,
    # and the wage query inner-joins Employee so a NULL drops out of payroll.
    with op.batch_alter_table("production_event") as batch:
        batch.alter_column("employee_id", existing_type=GUID(), nullable=True)


def downgrade() -> None:
    # Vendor-logged events have no employee, so they must go before the column
    # can be NOT NULL again — otherwise the alter fails on real data.
    op.execute("DELETE FROM production_event WHERE employee_id IS NULL")
    with op.batch_alter_table("production_event") as batch:
        batch.alter_column("employee_id", existing_type=GUID(), nullable=False)
    op.drop_constraint("fk_production_event_vendor_id_vendor",
                       "production_event", type_="foreignkey")
    op.drop_index("ix_production_event_vendor_id",
                  table_name="production_event")
    op.drop_column("production_event", "vendor_id")

    for ix in ("ix_job_work_piece_piece_status", "ix_job_work_piece_status",
               "ix_job_work_piece_piece_id", "ix_job_work_piece_job_work_id"):
        op.drop_index(ix, table_name="job_work_piece")
    op.drop_table("job_work_piece")
    for ix in ("ix_job_work_vendor_status", "ix_job_work_expected_back",
               "ix_job_work_status", "ix_job_work_stage", "ix_job_work_vendor_id"):
        op.drop_index(ix, table_name="job_work")
    op.drop_table("job_work")
    op.drop_index("ix_vendor_name", table_name="vendor")
    op.drop_table("vendor")
