"""Accessories belong to a SKU, and accessory kinds become data.

WHAT CHANGED, AND WHY

    A recipe line could be STYLE-WIDE, which forced `garment_size` into existence:
    a second, independent way to scope a line to a size. That column brought a
    size-coverage gate, a size-ambiguity gate, a "is this number a garment size"
    guess that confined a 60cm zip to 4XL jackets, and a PATCH that silently
    un-scoped a line.

    None of it is needed. A SKU is unique on (style_id, color_code, size) — it
    already carries colour AND size — so an accessory line that names its SKU has
    said everything there is to say about which garments it is for.

    This revision does not migrate recipe lines: the data is development-only by the
    owner's decision. It does DEACTIVATE any surviving style-wide accessory line,
    because such a row can no longer be written, cannot be satisfied, and would sit
    on the new release gate's coverage check forever.

    `garment_size` IS NOT DROPPED. Leather and lining can still be style-wide and
    still use it. It is simply never written for an accessory — the same "keep the
    column, stop writing it" pattern the retired drawer tables follow.

Revision ID: 20260929_accessory_sku_scope
Revises: 20260928_garment_size_backfill
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
revision = "20260929_accessory_sku_scope"
down_revision = "20260928_garment_size_backfill"
branch_labels = None
depends_on = None

def _spec_table():
    return sa.table(
        "style_material_spec",
        sa.column("id", UUID()),
        sa.column("category", sa.String),
        sa.column("sku_id", UUID()),
        sa.column("article", sa.String),
        sa.column("is_active", sa.Boolean),
    )


def deactivate_style_wide_accessories(bind) -> int:
    """Retire accessory lines that name no SKU. Returns how many.

    A MODULE-LEVEL FUNCTION so it can be tested, for the reason
    tests/integration/test_garment_size_backfill.py spells out: an alembic-built
    SQLite database rejects every INSERT (the baseline sets
    `server_default=sa.text('now()')` on 138 timestamp columns and SQLite has no
    `now()`), so a migration body cannot be exercised by running the chain. Inline,
    this would run for the first time on a real database.

    DEACTIVATED, NEVER DELETED. `piece_material_issue.spec_line_id` points at these
    rows and is the record of what a garment was ACTUALLY given; deleting them would
    orphan exactly the history the ledger exists to keep.
    """
    spec = _spec_table()
    rows = bind.execute(
        sa.select(spec.c.id).where(
            spec.c.category == "ACCESSORY",
            spec.c.sku_id.is_(None),
            spec.c.is_active.is_(True),
        )
    ).fetchall()
    for (row_id,) in rows:
        bind.execute(sa.update(spec).where(spec.c.id == row_id)
                     .values(is_active=False))
    return len(rows)


def upgrade() -> None:
    bind = op.get_bind()
    retired = deactivate_style_wide_accessories(bind)
    print(f"[20260929_accessory_sku_scope] deactivated {retired} style-wide "
          f"accessory line(s) — an accessory now names the SKU it is for, so "
          f"those rows could never be satisfied.")


def downgrade() -> None:
    """The deactivations do NOT come back.

    Re-activating them would restore rows the write path refuses to create, and it
    is not recoverable anyway: after the upgrade, a line this deactivated and a line
    a DM removed on purpose are the same row. Catalogue DDL is owned by the follow-up
    migration so it can be applied independently to already-stamped databases.
    """
    pass
