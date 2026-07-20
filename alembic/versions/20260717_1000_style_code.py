"""style.code — readable per-style handle for the wages rate API

Revision ID: 20260717_1000_style_code
Revises: 20260713_0900_sku_order_line
Create Date: 2026-07-17

WHAT THIS DOES
  1. style.code           new column, backfilled, then made unique+indexed.
  2. wage_run period idx  ix_wage_run_period_start / _period_end.

WHY style.code
  The wages rate API addresses styles by a readable code, never by uuid — the same
  contract as /production/scan taking sku_code. A style code is make_sku_code's
  first two segments:

      style code   JP-CLERMONT_VEST
      sku codes    JP-CLERMONT_VEST-DARK_BROWN-46
                   JP-CLERMONT_VEST-BLACK-48 ...

  so it is the exact prefix of every SKU code beneath it, and a manager reads it
  straight off a printed traveler. A Rate is per style x operation, so the rate
  picker must be style-level: a SKU picker would list 12 codes for one style that
  all write to the same Rate row.

WHY THE SLUG IS COPIED IN HERE INSTEAD OF IMPORTED
  A migration must reproduce its historical behaviour forever. Importing
  app.modules.clients.utlis.make_style_code would mean that editing _slug next year
  silently changes what this migration does on a fresh database, and the codes in a
  rebuilt dev DB would stop matching the codes in production. The duplication is the
  point. Keep the two in sync by hand; if _slug ever changes, that is a NEW data
  migration, not an edit to this one.

WHY IT RAISES ON A COLLISION INSTEAD OF SKIPPING
  _slug is lossy: 'CLERMONT + VEST' and 'CLERMONT VEST' in the same order both
  become JP-CLERMONT_VEST. Leaving the loser NULL would let the migration pass
  quietly and then hide that style from GET /wages/styles — so its rates can never
  be set, so every piece-rate worker on it earns zero. Silent underpayment is worse
  than a failed migration. This is payroll; fail loud.

  Note this collision already exists today at the SKU level — make_sku_code collapses
  those two names identically, so sku.code's unique index already breaks on them.
  This constraint surfaces the same data problem one step earlier, with an error that
  names the style.

BEFORE RUNNING — the chain must have exactly ONE head.
  add_material_rate and fiber_role_bom_item both ship with down_revision = None,
  which makes them extra roots; `alembic upgrade head` fails with "multiple heads"
  until they are chained:
      add_material_rate.down_revision    = "20260704_1500_config_client_checks"
      fiber_role_bom_item.down_revision  = "add_material_rate"
  Then confirm this file's down_revision against `alembic heads` — the two most
  recent migrations (20260712_1000_piece_tracking, 20260713_0900_sku_order_line)
  were not in the dump I verified against, so the value below is an assumption, not
  a checked fact.
"""
import re

import sqlalchemy as sa
from alembic import op

revision = "20260717_1000_style_code"
<<<<<<< HEAD
down_revision = "20260714_1000_order_num_uniq"   # VERIFY with `alembic heads`
=======
down_revision = "20260714_1000_order_num_uniq" # VERIFY with `alembic heads`
>>>>>>> ismail-dev
branch_labels = None
depends_on = None


# ── frozen copy of clients.utlis._slug / make_style_code — see module docstring ──
def _slug(s: str | None) -> str:
    """UPPER, runs of non-alnum -> single '_', trimmed. '' -> 'NA'."""
    out = re.sub(r"[^A-Za-z0-9]+", "_", (s or "").strip()).strip("_").upper()
    return out or "NA"


def _make_style_code(order_number: str | None, style_name: str | None) -> str:
    return "-".join((_slug(order_number), _slug(style_name)))


def upgrade() -> None:
    # ── 1. style.code ───────────────────────────────────────────────────────
    # Added nullable so existing rows survive the ALTER; backfilled below; the
    # unique index goes on last, once every row has a value. Adding it NOT NULL
    # up front would fail on the first existing style.
    op.add_column("style", sa.Column("code", sa.String(200), nullable=True))

    bind = op.get_bind()
    rows = bind.execute(sa.text("""
        SELECT s.id, s.name, o.order_number
          FROM style s
          JOIN client_order o ON o.id = s.client_order_id
         WHERE s.code IS NULL
    """)).fetchall()

    taken: dict[str, str] = {}
    updates: list[dict] = []
    clashes: list[tuple[str, str, str]] = []

    for style_id, name, order_number in rows:
        code = _make_style_code(order_number, name)
        if code in taken:
            clashes.append((code, taken[code], name or "(null)"))
            continue
        taken[code] = name or "(null)"
        updates.append({"code": code, "sid": style_id})

    if clashes:
        detail = "\n".join(
            f"    {code}  <-  '{kept}'  AND  '{dropped}'" for code, kept, dropped in clashes
        )
        raise RuntimeError(
            f"\n{len(clashes)} style-code collision(s). These style names slug to the "
            f"same code within one order:\n{detail}\n\n"
            "  Rename one style of each pair in the source data (they are already "
            "colliding at the SKU level today — make_sku_code collapses them the same "
            "way), then re-run this migration.\n"
            "  Nothing has been committed; the transaction rolls back.\n"
        )

    if updates:
        bind.execute(
            sa.text("UPDATE style SET code = :code WHERE id = :sid"), updates
        )

    # Unique now that every row is populated. Left nullable at the column level:
    # a style created by a code path that forgets to set it should fail on the
    # index (loud, named) rather than on a NOT NULL (loud, anonymous), and NULLs
    # do not collide in a Postgres unique index.
    op.create_index("ix_style_code", "style", ["code"], unique=True)

    # ── 2. wage_run period indexes ──────────────────────────────────────────
    # Every compute_run does an overlap check filtering status + both period
    # columns. At this table's scale (hundreds of rows for years) Postgres will
    # seq-scan regardless — these exist so the models and the DB do not drift and
    # the next autogenerate produces an empty diff, not because payroll is slow.
    op.create_index("ix_wage_run_period_start", "wage_run", ["period_start"])
    op.create_index("ix_wage_run_period_end", "wage_run", ["period_end"])


def downgrade() -> None:
    op.drop_index("ix_wage_run_period_end", table_name="wage_run")
    op.drop_index("ix_wage_run_period_start", table_name="wage_run")
    op.drop_index("ix_style_code", table_name="style")
    op.drop_column("style", "code")
