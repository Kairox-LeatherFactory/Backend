"""an accessory line can name WHICH GARMENT SIZES it belongs to

Revision ID: 20260903_garment_size
Revises: 20260902_store_piece
Create Date: 2026-09-03

THE BUG
──────────────────────────────────────────────────────────────────────────────
A DM assigned Thread S, Thread M and Thread L to a style. The store then showed
all three on every garment, whatever its size, and the kit scan spent all three.

`StyleSpecService.merge_lines` keyed the effective recipe on
(category, subtype, article) and never read SKU.size, so:

    three distinct ARTICLES  -> three distinct keys -> all three survive, on
                                every piece of every size
    one article, three SIZES -> one key             -> only the last survives,
                                silently

Both readings are wrong and neither is detectable from the outside.

WHY A NEW COLUMN AND NOT THE EXISTING `size`
──────────────────────────────────────────────────────────────────────────────
`style_material_spec.size` already exists and means the MATERIAL's size — the
column's own comment says "zip 60cm, button 18L" — and it is fed to
MaterialLot.size to resolve which lot to spend. Overloading it to also mean the
garment's size would make a 60cm zip look like a garment size and break lot
resolution for every accessory that has a real size of its own.

So `garment_size` is its own column. NULL means "every size", which is what every
existing line means today — hence no backfill and no behaviour change for any
style already released.

THE CONVENIENCE THAT MAKES IT AUTOMATIC
──────────────────────────────────────────────────────────────────────────────
In this factory an accessory that varies by garment size is labelled with the
GARMENT's size: a "zip L" is the zip for an L jacket. So when a DM adds a line
whose material `size` is a garment-size token (S/M/L/XL/XXL, or 38-62),
StyleSpecService defaults `garment_size` to it. The DM keeps entering exactly
what they entered before and the matching starts working — see
_looks_like_a_garment_size.

PORTABILITY (CLAUDE.md §13): VARCHAR, not a native enum. No JSONB, no ON
CONFLICT. The unique constraint is rebuilt to include the new column, named
explicitly to match NAMING_CONVENTION.
"""
from alembic import op
import sqlalchemy as sa

revision = "20260903_garment_size"
down_revision = "20260902_store_piece"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("style_material_spec",
                  sa.Column("garment_size", sa.String(length=40), nullable=True))
    op.create_index("ix_style_material_spec_garment_size",
                    "style_material_spec", ["garment_size"])

    # The uniqueness BACKSTOP has to include the new column, or two lines that
    # differ only by garment size collide. (It is only a backstop — four of these
    # columns are nullable and NULLs compare distinct on both Postgres and
    # SQLite, so StyleSpecRepository.find_duplicate_line remains the real check.)
    with op.batch_alter_table("style_material_spec") as batch:
        batch.drop_constraint("uq_style_material_spec_line", type_="unique")
        batch.create_unique_constraint(
            "uq_style_material_spec_line",
            ["style_id", "sku_id", "category", "subtype", "article",
             "colour", "thickness", "size", "garment_size"])


def downgrade() -> None:
    with op.batch_alter_table("style_material_spec") as batch:
        batch.drop_constraint("uq_style_material_spec_line", type_="unique")
        batch.create_unique_constraint(
            "uq_style_material_spec_line",
            ["style_id", "sku_id", "category", "subtype", "article",
             "colour", "thickness", "size"])
    op.drop_index("ix_style_material_spec_garment_size",
                  table_name="style_material_spec")
    op.drop_column("style_material_spec", "garment_size")
