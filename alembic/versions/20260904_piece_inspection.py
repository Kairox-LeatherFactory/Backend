"""stage-wise reject & rework, with who is answerable for it

Revision ID: 20260904_inspection
Revises: 20260903_garment_size
Create Date: 2026-09-04

THE GAP
──────────────────────────────────────────────────────────────────────────────
"A piece completed in Fusing was rejected during Pasting, but there is currently
no proper way to send it back to Fusing for rework."

There was not. The sequence gate treats a completed stage as complete for good,
re-logging it lands in the `rework` bucket and writes nothing, and the only route
back was an edit in the database. So defects were handled by telling somebody,
and nothing was ever counted.

TWO STEPS, BECAUSE A GARMENT MOVING BACKWARDS IS NOT A FLOOR DECISION
Anyone standing at a stage can see a defect, so any manager or HR may raise one.
Moving the piece re-opens a completed stage, re-orders work and can cost
material — so the DM signs it off. Until then the rejection is a report.

WHO IS RESPONSIBLE IS A COLUMN
The factory's own requirement: if an employee did not cut properly, or a stage
was not done properly and the product is damaged because of it, that employee is
responsible for that piece.

A sentence in a reason box cannot be counted across a month, produced in a wage
conversation, or separate a bad hide (the supplier's problem) from bad work (a
training or pay problem). So the defect carries a TYPE, and when it is
workmanship, the employee and the stage they were doing.

production_event.is_rework SPLITS THE COST
"This order cost X, of which Y was rework" is not answerable from one
consumption column. The second cut of a re-made panel is real leather spent but
is not what the garment was supposed to cost, and averaging the two hides how
much the floor loses to defects. Grouping consumption_qty by this flag gives
both numbers from one table, with no second quantity to keep in step.

PORTABILITY (CLAUDE.md §13): every status column is VARCHAR, not a native enum,
so adding a verdict or a defect type never needs an ALTER TYPE. No JSONB, no
ON CONFLICT, ids from uuid4. Constraints named to match NAMING_CONVENTION.
"""
from alembic import op
import sqlalchemy as sa

from sqlalchemy.dialects.postgresql import UUID as GUID

revision = "20260904_inspection"
down_revision = "20260903_garment_size"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    false_ = sa.text("0" if bind.dialect.name == "sqlite" else "false")

    op.add_column("production_event",
                  sa.Column("is_rework", sa.Boolean(), nullable=False,
                            server_default=false_))
    op.create_index("ix_production_event_is_rework", "production_event",
                    ["is_rework"])

    op.create_table(
        "piece_inspection",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("piece_id", GUID(), nullable=True),
        # WHERE IT WAS FOUND, not where it was caused. A bad fuse is found at
        # pasting; keeping the two apart is what makes "which stage causes the
        # most rework" answerable at all.
        sa.Column("found_at_stage", sa.String(length=30), nullable=False),
        sa.Column("verdict", sa.String(length=10), nullable=False),
        sa.Column("action", sa.String(length=10), nullable=True),
        sa.Column("return_to_stage", sa.String(length=30), nullable=True),
        sa.Column("defect_type", sa.String(length=20), nullable=True),
        # NULL for PRODUCT_DAMAGE — nobody on the floor is answerable for a bad
        # hide, and naming somebody would be worse than naming nobody.
        sa.Column("responsible_employee_id", GUID(), nullable=True),
        sa.Column("responsible_stage", sa.String(length=30), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False,
                  server_default="PENDING"),
        # app_user ids — the LOGIN. Passing a scanned employee.id into an actor
        # column is what 500'd the store scan.
        sa.Column("raised_by", GUID(), nullable=True),
        sa.Column("raised_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", GUID(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.String(length=500), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_piece_inspection"),
        sa.ForeignKeyConstraint(["piece_id"], ["piece.id"], ondelete="SET NULL",
                                name="fk_piece_inspection_piece_id_piece"),
        sa.ForeignKeyConstraint(
            ["responsible_employee_id"], ["employee.id"], ondelete="SET NULL",
            name="fk_piece_inspection_responsible_employee_id_employee"),
        sa.ForeignKeyConstraint(["raised_by"], ["app_user.id"],
                                ondelete="SET NULL",
                                name="fk_piece_inspection_raised_by_app_user"),
        sa.ForeignKeyConstraint(["decided_by"], ["app_user.id"],
                                ondelete="SET NULL",
                                name="fk_piece_inspection_decided_by_app_user"),
    )
    op.create_index("ix_piece_inspection_piece_id", "piece_inspection",
                    ["piece_id"])
    op.create_index("ix_piece_inspection_found_at_stage", "piece_inspection",
                    ["found_at_stage"])
    op.create_index("ix_piece_inspection_defect_type", "piece_inspection",
                    ["defect_type"])
    op.create_index("ix_piece_inspection_responsible_employee_id",
                    "piece_inspection", ["responsible_employee_id"])
    op.create_index("ix_piece_inspection_status", "piece_inspection", ["status"])
    # The open-rejections lookup the production log runs on every scan.
    op.create_index("ix_piece_inspection_piece_status", "piece_inspection",
                    ["piece_id", "status"])


def downgrade() -> None:
    for ix in ("ix_piece_inspection_piece_status",
               "ix_piece_inspection_status",
               "ix_piece_inspection_responsible_employee_id",
               "ix_piece_inspection_defect_type",
               "ix_piece_inspection_found_at_stage",
               "ix_piece_inspection_piece_id"):
        op.drop_index(ix, table_name="piece_inspection")
    op.drop_table("piece_inspection")
    op.drop_index("ix_production_event_is_rework", table_name="production_event")
    op.drop_column("production_event", "is_rework")
