"""One lot per material spec (Option A).

A material lot identifies WHAT the material is — not when it was bought. Buying
the same article/colour/thickness again is a TOP-UP of the existing lot through
POST /materials/receive, never a second row.

WHY THIS EXISTS AS A DB CONSTRAINT AND NOT JUST A SERVICE CHECK
    MaterialService.create_lot already rejects a duplicate with a 409 that points
    at the existing lot. That is the good error message, but it is a read
    followed by a write: two managers adding the same delivery at the same
    moment both see "no duplicate" and both insert. This index is what makes the
    rule actually true, which the lot picker depends on — "filter article +
    colour + thickness → one row" has to be a guarantee, not a coincidence.

WHY PARTIAL (WHERE is_active)
    Deactivating a lot is how a material is retired. A retired lot must not
    block re-creating that spec later, and two retired lots of the same spec are
    a normal historical outcome.

WHY NULLS NOT DISTINCT
    colour / thickness / size / subtype are nullable, and by default Postgres
    treats every NULL as distinct — so without this, unlimited lots with a blank
    thickness could coexist, which is exactly the case a leather lot hits. Two
    lots that both leave thickness blank ARE the same material. Requires PG 15+;
    the live database is 17.6.

SQLITE
    Skipped entirely off Postgres: SQLite has no NULLS NOT DISTINCT, and the
    test suite is single-threaded so the service-level check is sufficient there.

VERIFIED BEFORE WRITING: 0 duplicate rows on this key on the live database
(active or otherwise), so the index builds without a backfill.

Revision ID: 20260810_unique_material_lot
Revises: drawer_pool_bootstrap
"""
from alembic import op

revision = "20260810_unique_material_lot"
down_revision = "drawer_pool_bootstrap"
branch_labels = None
depends_on = None

INDEX_NAME = "uq_material_lot_spec"


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS {INDEX_NAME}
        ON material_lot (category, subtype, article, colour, thickness, size)
        NULLS NOT DISTINCT
        WHERE is_active
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
