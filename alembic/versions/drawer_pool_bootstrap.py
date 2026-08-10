"""drawer pool bootstrap + legacy state normalisation

Revision ID: drawer_pool_bootstrap
Revises: <PUT_CURRENT_HEAD_HERE>
Create Date: 2026-08-08

WHAT THIS DOES
--------------------------------------------------------------------------------
The drawer-redesign build changes drawers from "one fresh drawer per piece" to a
FIXED, RECYCLING POOL of 200 permanent barcoded drawers (growing on demand).

This migration is DATA, not schema — the `drawer` and `barcode_registry` tables
already exist. It:

  1. Bootstraps the 200-drawer pool IF the table is empty (fresh environments).
     On an environment that already has drawers from the old per-piece scheme,
     it does NOT mint 200 more — the old drawers are kept; see the note below.

  2. Is written portably (no Postgres-only syntax) so it also runs on SQLite in
     CI. No ::uuid casts, no ON CONFLICT, no gen_random_uuid().

MIGRATING AN ENVIRONMENT THAT ALREADY RAN THE OLD SCHEME
--------------------------------------------------------------------------------
Old drawers were 1:1 with pieces and never recycled. If you have such data, the
clean path is a one-time reconciliation (out of band, not in this migration,
because it depends on how far each piece has progressed):

  - drawers whose piece has shipped (PACKAGE done) → set state='waiting',
    current_piece_id=NULL (return to pool)
  - keep the rest as-is; they drain naturally as pieces finish.

Then run scripts/bootstrap_drawers.py to top the pool up to 200. This migration
deliberately does NOT guess that reconciliation.

NOTE ON GUID PORTABILITY
    We insert via the SQLAlchemy core table with server-agnostic values so GUID()
    (CHAR(32) off Postgres) and native uuid on Postgres both work. We generate
    ids in Python.
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "drawer_pool_bootstrap"
# Rebased onto the role-enum case fix (was 20260804_role_values). Both migrations
# had claimed that same parent, which makes two heads and fails `upgrade head`.
# The role fix is the urgent one, so it goes first and this stacks on top.
down_revision = "20260810_role_case"
branch_labels = None
depends_on = None

INITIAL_POOL = 200


def _table(name: str) -> sa.Table:
    return sa.Table(name, sa.MetaData(), autoload_with=op.get_bind())


def upgrade() -> None:
    bind = op.get_bind()
    drawer = _table("drawer")
    registry = _table("barcode_registry")

    existing = bind.execute(sa.select(sa.func.count()).select_from(drawer)).scalar() or 0
    if existing > 0:
        # Environment already has drawers (old scheme or a prior bootstrap).
        # Do nothing here — see the module docstring for the reconciliation path.
        return

    now = sa.func.now()
    drawer_rows = []
    registry_rows = []
    for seq in range(1, INITIAL_POOL + 1):
        did = uuid.uuid4()
        code = f"DRW-{seq:04d}"
        drawer_rows.append({
            "id": did, "code": code, "seq": seq, "state": "waiting",
            "current_piece_id": None, "leather_in": False, "lining_in": False,
            "received_at": None, "sended_at": None,
            "created_at": None, "updated_at": None,
        })
        registry_rows.append({
            "id": uuid.uuid4(), "code": code, "type": "DRAWER",
            "status": "active", "drawer_id": did, "caption": f"Drawer {seq}",
            "piece_id": None, "order_id": None, "sku_id": None, "style_id": None,
            "employee_id": None, "material_lot_id": None,
            "retired_at": None, "retired_reason": None,
            "created_at": None, "updated_at": None,
        })

    # Insert only the columns that actually exist on each table (defensive: the
    # dump shows TimestampMixin/UUIDMixin, but column presence is confirmed at
    # runtime via the reflected table).
    _bulk_insert(bind, drawer, drawer_rows)
    _bulk_insert(bind, registry, registry_rows)


def _bulk_insert(bind, table: sa.Table, rows: list[dict]) -> None:
    cols = set(table.c.keys())
    cleaned = [{k: v for k, v in r.items() if k in cols} for r in rows]
    if cleaned:
        bind.execute(table.insert(), cleaned)


def downgrade() -> None:
    bind = op.get_bind()
    drawer = _table("drawer")
    registry = _table("barcode_registry")
    # Remove ONLY the bootstrapped pool drawers (DRW-0001..DRW-0200) that are
    # still WAITING and unoccupied — never delete a drawer that holds a piece.
    codes = [f"DRW-{seq:04d}" for seq in range(1, INITIAL_POOL + 1)]
    bind.execute(
        registry.delete().where(
            registry.c.code.in_(codes),
            registry.c.type == "DRAWER",
        )
    )
    bind.execute(
        drawer.delete().where(
            drawer.c.code.in_(codes),
            drawer.c.state == "waiting",
            drawer.c.current_piece_id.is_(None),
        )
    )