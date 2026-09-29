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

ACCESSORY KINDS

    `MaterialSubtype` offered BUTTON / ZIP / THREAD / OTHER, so eyelets, lace pins
    and rib knit trim all fell to OTHER — whose spec requires `description` + `count`
    and whose filters are `article, colour` with NO SIZE AT ALL. Rib knit trim, an
    accessory whose size genuinely varies per SKU, therefore had no size field to
    vary.

    `accessory_type` makes the kinds data, seeded with the four built-ins plus the
    three the floor named. It fills itself in from then on: receiving a packet of
    something unrecognised registers it (MaterialService.arrive / .create_lot), which
    is the only moment anybody actually knows about a new accessory.

    `code` IS String(20) TO MATCH `subtype` on material_lot, style_material_spec,
    piece_material_issue and kit_substitution_request. Capping the source is cheaper
    and safer than widening four columns, one of which is a ledger.

Revision ID: 20260929_accessory_sku_scope
Revises: 20260928_garment_size_backfill
"""
from alembic import op
import sqlalchemy as sa

from app.core.models import GUID

revision = "20260929_accessory_sku_scope"
down_revision = "20260928_garment_size_backfill"
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(
    sa.dialects.postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def _spec_table():
    return sa.table(
        "style_material_spec",
        sa.column("id", GUID()),
        sa.column("category", sa.String),
        sa.column("sku_id", GUID()),
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


def seed_accessory_types(bind) -> int:
    """Insert the starting kinds, skipping any already present. Returns how many.

    IDEMPOTENT, so a re-run or a replayed backup adds nothing. The list lives in
    materials/accessory_catalog.SEED_TYPES so this and scripts/seed.py read one
    source rather than drifting.
    """
    from app.modules.materials.accessory_catalog import SEED_TYPES
    import uuid
    from datetime import datetime, timezone

    table = sa.table(
        "accessory_type",
        sa.column("id", GUID()),
        sa.column("code", sa.String),
        sa.column("label", sa.String),
        sa.column("qty_field", sa.String),
        sa.column("qty_uom", sa.String),
        sa.column("requires", _JSON),
        sa.column("filters", _JSON),
        sa.column("size_varies_by_sku", sa.Boolean),
        sa.column("is_active", sa.Boolean),
        sa.column("first_seen_at", sa.DateTime(timezone=True)),
        sa.column("created_by", sa.String),
        sa.column("note", sa.String),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    existing = {r[0] for r in bind.execute(sa.select(table.c.code)).fetchall()}
    now = datetime.now(timezone.utc)
    added = 0
    for t in SEED_TYPES:
        if t["code"] in existing:
            continue
        bind.execute(table.insert().values(
            id=uuid.uuid4(), code=t["code"], label=t["label"],
            qty_field=t["qty_field"], qty_uom=t["qty_uom"],
            requires=t["requires"], filters=t["filters"],
            size_varies_by_sku=t["size_varies_by_sku"], is_active=True,
            first_seen_at=now, created_by="seed",
            note="Seeded with the accessory catalogue.",
            created_at=now, updated_at=now))
        added += 1
    return added


def upgrade() -> None:
    op.create_table(
        "accessory_type",
        sa.Column("id", GUID(), primary_key=True),
        # ≤20 chars to match `subtype` on four tables — see the module docstring.
        sa.Column("code", sa.String(20), nullable=False),
        sa.Column("label", sa.String(80), nullable=True),
        # THE MEASUREMENT: which attribute holds the quantity, and its unit. There is
        # no separate "measurement" column because the quantity field IS the
        # measurement — count for buttons, mtrs for thread, kg for ribs.
        sa.Column("qty_field", sa.String(20), nullable=False,
                  server_default="count"),
        sa.Column("qty_uom", sa.String(20), nullable=False, server_default="pcs"),
        sa.Column("requires", _JSON, nullable=True),
        sa.Column("filters", _JSON, nullable=True),
        # The floor's own observation as data: ~85-90% of accessories are identical
        # across a style's SKUs; zip and rib knit trim are the ones that vary.
        sa.Column("size_varies_by_sku", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("is_active", sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        # How an auto-registered kind is told apart from a curated one. The gate is
        # the least supervised entry in the app, so a kind that appears by accident
        # must be visible rather than silent.
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(120), nullable=True),
        sa.Column("note", sa.String(300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("code", name="uq_accessory_type_code"),
    )
    op.create_index("ix_accessory_type_code", "accessory_type", ["code"])

    bind = op.get_bind()
    seeded = seed_accessory_types(bind)
    retired = deactivate_style_wide_accessories(bind)
    print(f"[20260929_accessory_sku_scope] seeded {seeded} accessory kind(s); "
          f"deactivated {retired} style-wide accessory line(s) — an accessory now "
          f"names the SKU it is for, so those rows could never be satisfied.")


def downgrade() -> None:
    """The table goes; the deactivations do NOT come back.

    Re-activating them would restore rows the write path refuses to create, and it
    is not recoverable anyway: after the upgrade, a line this deactivated and a line
    a DM removed on purpose are the same row.
    """
    op.drop_index("ix_accessory_type_code", table_name="accessory_type")
    op.drop_table("accessory_type")
