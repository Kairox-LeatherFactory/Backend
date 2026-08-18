"""Drop drawer.sent_to — the store has only one way forward.

WHY IT EXISTED AND WHY IT SHOULD NOT
    20260813_bugfix_v1 added `drawer.sent_to` so a batch send could record
    whether a drawer went to the lining floor or the stitching floor. That came
    from the bug spec's wording, "send selected Drawers to Lining or Stitching",
    taken literally instead of checked against the pipeline.

    The pipeline says otherwise:

        leather cut ─┐
                     ├─► drawer (merge) ─► LINE_STITCHING ─► SHELL_STITCHING
        lining cut ──┘                     ─► FINAL_FINISH ─► INSPECTION ─► PACKAGE

    Lining is UPSTREAM of the store: the lining is cut and then scanned INTO the
    drawer, which is the whole point of the merge. A drawer holding both parts
    therefore has exactly one destination, and "send to lining" would mean
    routing a garment backwards to a stage it has already cleared.

    So the column could only ever hold one value, and `state == SENDED` already
    carries that meaning. A column whose only possible value is a constant is a
    question the schema should not be asking, and leaving it would invite the
    next person to build a routing feature on top of a distinction that does not
    exist on the floor.

DATA LOSS
    None that matters. The column was live for less than a day, only ever written
    with 'STITCHING', and nothing reads it — the drawer's lifecycle position is
    the real record and is untouched.

DOWNGRADE restores the column (nullable, indexed) but not its values; the
information it held is fully recoverable from `state` and `sended_at`.

Revision ID: 20260813_drop_sent_to
Revises: 20260813_bugfix_v1
"""
import sqlalchemy as sa
from alembic import op

revision = "20260813_drop_sent_to"
down_revision = "20260813_bugfix_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("drawer")}
    if "sent_to" not in cols:
        return                      # never applied, or already dropped

    # Drop the index first: SQLite refuses to drop a column an index still
    # references, and on Postgres the index would go with the column anyway.
    existing = {ix["name"] for ix in insp.get_indexes("drawer")}
    if "ix_drawer_sent_to" in existing:
        op.drop_index("ix_drawer_sent_to", table_name="drawer")
    op.drop_column("drawer", "sent_to")


def downgrade() -> None:
    op.add_column("drawer", sa.Column("sent_to", sa.String(length=20),
                                      nullable=True))
    op.create_index("ix_drawer_sent_to", "drawer", ["sent_to"])
