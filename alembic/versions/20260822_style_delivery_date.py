"""Style.delivery_date — the ship date the breakdown sheet prints per style.

THE ONLY SCHEMA CHANGE IN THIS TASK. Everything else that ships alongside it —
arithmetic size-band detection, price extraction — writes into columns that
already exist (`style.unit_price`, `style.currency`).

WHY A NEW COLUMN AND NOT ClientOrder.delivery_deadline
    They are different facts. `delivery_deadline` is the ORDER's commercial
    deadline. This is the ship date printed on one STYLE's row, and a single
    order sheet routinely carries a main-season style and an outlet style with
    different dates. Folding them onto the order would hide the earlier one and
    misstate the later one, so `delivery_deadline` is left untouched.

NULLABLE, NO BACKFILL, AND THE NULL IS ORDINARY
    Unlike the three-state flags elsewhere in this schema, a NULL here carries no
    special meaning beyond "this sheet had no DELIVERY column" — which is the
    normal case for the John Peter consolidated sheets. It is not an error state
    and nothing downstream treats it as one.

PORTABILITY (CLAUDE.md §13): a plain nullable DATE. No enum, no JSONB, no
ON CONFLICT, no gen_random_uuid(); identical on SQLite and Postgres, and fully
transactional.

Revision ID: 20260822_style_delivery
Revises: 20260821_style_spec
"""
from alembic import op
import sqlalchemy as sa

revision = "20260822_style_delivery"
down_revision = "20260821_style_spec"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("style", sa.Column("delivery_date", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("style", "delivery_date")
