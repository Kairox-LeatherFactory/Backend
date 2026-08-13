"""Production-logger / store-hub bug fixes v1: compact piece codes, drawer
routing, and the STORE_MANAGER role.

THREE INDEPENDENT SCHEMA MOVES, ONE REVISION — they ship together because the
frontend cutover for bugs-in-production-v1 needs all three at once.

1. barcode_registry.is_alias  (bug #19)
   A piece now holds TWO codes: the compact `PC-…` that is printed and scanned
   (PRIMARY) and its long legacy code, kept ACTIVE so labels already on the floor
   still resolve (ALIAS). Every per-order count filters on this flag; without it
   `minted` doubles and the duplicates=0 integrity proof reads as broken.
   Backfilled False, which is correct for every existing row: they are all
   primaries today. `scripts/backfill_short_codes.py` is what flips them, and it
   is deliberately a SCRIPT, not this migration — it mints new rows and moves
   FKs across ~1,400 rows per order, which is data movement that wants to be
   re-runnable and interruptible, not wedged inside a schema lock.

2. drawer.sent_to  (bugs #13/#14)
   Where a batch send routed the drawer: STITCHING (released past the merge gate)
   or LINING. `state` records THAT a drawer was sent; it cannot record where.

3. user_role += 'STORE_MANAGER'  (bug #16)
   ADD THE MEMBER NAME, UPPERCASE. `Enum(UserRole, name="user_role")` with no
   values_callable persists the member NAME, so the driver sends 'STORE_MANAGER'.
   Adding the lowercase 'store_manager' would create a label the ORM never emits
   and the role would stay unusable — the INSERT would still die with
   `invalid input value for enum user_role: "STORE_MANAGER"`. This exact trap
   already bit lining_manager, security and merchandiser (see CLAUDE.md §13 and
   20260810_role_case, which repaired all three).

   F63 (as in 20260730_barcode / 20260804_role_values / 20260810_role_case):
   ALTER TYPE ... ADD VALUE cannot run inside the transaction Alembic wraps a
   migration in, so it goes in an autocommit_block(). That commits the label on
   its own; if a later step in this revision fails, the label survives — which is
   harmless and re-runnable, because ADD VALUE IF NOT EXISTS is idempotent.

DOWNGRADE drops the two columns. It does NOT remove the enum label: Postgres
cannot drop a value from a native enum type, which is the whole reason §13
exists. Left explicit rather than silently omitted.

Revision ID: 20260813_bugfix_v1
Revises: 20260810_unique_material_lot
"""
import sqlalchemy as sa
from alembic import op

revision = "20260813_bugfix_v1"
down_revision = "20260810_unique_material_lot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # ── 1. the alias flag ────────────────────────────────────────────────────
    # server_default is required: the table is populated, and the column is NOT
    # NULL. Dropped again afterwards on Postgres so the application default (and
    # only the application default) governs new rows — SQLite cannot ALTER a
    # column, and leaving the default there is harmless on a test-only dialect.
    op.add_column(
        "barcode_registry",
        sa.Column("is_alias", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
    )
    op.create_index("ix_barcode_registry_is_alias", "barcode_registry",
                    ["is_alias"])
    if is_pg:
        op.alter_column("barcode_registry", "is_alias", server_default=None)

    # ── 2. drawer routing ────────────────────────────────────────────────────
    op.add_column("drawer", sa.Column("sent_to", sa.String(length=20),
                                      nullable=True))
    op.create_index("ix_drawer_sent_to", "drawer", ["sent_to"])

    # ── 3. the new role label ────────────────────────────────────────────────
    # Guarded so it is a no-op on SQLite, which has no native enum type.
    if is_pg:
        with op.get_context().autocommit_block():
            op.execute(
                "ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'STORE_MANAGER'")


def downgrade() -> None:
    op.drop_index("ix_drawer_sent_to", table_name="drawer")
    op.drop_column("drawer", "sent_to")
    op.drop_index("ix_barcode_registry_is_alias", table_name="barcode_registry")
    op.drop_column("barcode_registry", "is_alias")
    # 'STORE_MANAGER' stays on the user_role type: Postgres cannot drop an enum
    # value. Any app_user row still carrying it would be orphaned by a removal
    # even if it were possible.
