"""
INTEGRATION · the migration that undoes the inferred garment_size.

WHY A TEST FOR A MIGRATION. This repo's alembic chain cannot run on SQLite — an
early revision uses create_foreign_key, which SQLite has no ALTER for — so without
this the data fix would run for the very first time on the production database.
A one-way UPDATE over live recipe rows is the last place to find out you got the
predicate wrong: after it runs, a cleared line and a line somebody deliberately
left unscoped are the same row.

WHAT IT HAS TO GET RIGHT, and both directions cost money:

  · CLEAR the numeric misfires. '60' is on the EU jacket ladder, so a 60cm zip
    entered as size '60' was read as a size-60 garment and scoped to 4XL. Every
    other size then has no zip line at all — which is not a short kit. It is
    kit_required=False, piece_complete collapsing to leather-and-lining, and the
    garment shipping without a zip.
  · LEAVE the alpha ones alone. 'L' beside garment_size 'L' is a line a human
    meant. Clearing it would widen a genuinely size-specific button to every
    garment, and that error spends stock rather than skipping it.
"""
import importlib.util
import pathlib
import uuid

import pytest
from sqlalchemy import select

from app.modules.barcode.models import StyleMaterialSpec

pytestmark = pytest.mark.asyncio

_MIGRATION = (pathlib.Path(__file__).resolve().parents[2]
              / "alembic" / "versions" / "20260927_kit_packet_scan.py")


def _load():
    """Import the migration by path — it is not on the import path as a module."""
    spec = importlib.util.spec_from_file_location("mig_kit_packet", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def _line(db, style_id, **kw):
    base = dict(style_id=style_id, category="ACCESSORY", subtype="ZIP",
                article="ZIP-N", colour="BLACK", qty_per_piece=1, uom="pcs")
    base.update(kw)
    row = StyleMaterialSpec(id=uuid.uuid4(), **base)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def _run(db):
    """Run the migration's data fix against this session's connection."""
    mod = _load()
    conn = await db.connection()
    return await conn.run_sync(lambda sync_conn:
                               mod.clear_inferred_garment_sizes(sync_conn))


async def test_a_numeric_material_size_is_freed_from_its_inferred_scope(
        db, order_tree):
    """THE MISFIRE. A 60cm zip was scoped to size-60 (4XL) garments."""
    style = order_tree["style"]
    row = await _line(db, style.id, size="60", garment_size="60")

    cleared = await _run(db)

    assert cleared == 1
    await db.refresh(row)
    assert row.garment_size is None, "a 60cm zip belongs on every garment"


async def test_an_alpha_size_is_left_exactly_as_the_dm_entered_it(
        db, order_tree):
    """'L' next to garment_size 'L' is somebody's decision, not a guess."""
    style = order_tree["style"]
    row = await _line(db, style.id, size="L", garment_size="L")

    cleared = await _run(db)

    assert cleared == 0
    await db.refresh(row)
    assert row.garment_size == "L"


async def test_an_explicitly_different_garment_size_is_not_touched(
        db, order_tree):
    """A 60cm zip the DM deliberately scoped to L garments stays scoped to L —
    the two values differing is the proof a human set it."""
    style = order_tree["style"]
    row = await _line(db, style.id, size="60", garment_size="L")

    cleared = await _run(db)

    assert cleared == 0
    await db.refresh(row)
    assert row.garment_size == "L"


async def test_leather_and_lining_lines_are_out_of_scope(db, order_tree):
    """The inference only ever ran the accessory path into trouble, and a
    narrower UPDATE is a safer UPDATE."""
    style = order_tree["style"]
    row = await _line(db, style.id, category="LEATHER", subtype=None,
                      article="COW-1", thickness="1.2mm", size="52",
                      garment_size="52", uom="dcm")

    cleared = await _run(db)

    assert cleared == 0
    await db.refresh(row)
    assert row.garment_size == "52"


async def test_a_line_with_no_garment_size_is_already_correct(db, order_tree):
    style = order_tree["style"]
    row = await _line(db, style.id, size="60CM", garment_size=None)

    assert await _run(db) == 0
    await db.refresh(row)
    assert row.garment_size is None


async def test_it_is_idempotent(db, order_tree):
    """A migration that is run twice — a re-run, a restored backup replayed —
    must not find new work the second time."""
    style = order_tree["style"]
    await _line(db, style.id, size="60", garment_size="60")

    assert await _run(db) == 1
    assert await _run(db) == 0


async def test_a_mixed_recipe_clears_only_the_misfires(db, order_tree):
    """The realistic case: one style carrying all four shapes at once."""
    style = order_tree["style"]
    misfire = await _line(db, style.id, size="60", garment_size="60")
    meant = await _line(db, style.id, subtype="BUTTON", article="BTN-4H",
                        size="L", garment_size="L")
    explicit = await _line(db, style.id, subtype="THREAD", article="TH-40",
                           thickness="40", size="45", garment_size="S")
    general = await _line(db, style.id, subtype="OTHER", article="TAG",
                          size=None, garment_size=None)

    assert await _run(db) == 1

    for row in (misfire, meant, explicit, general):
        await db.refresh(row)
    assert misfire.garment_size is None
    assert meant.garment_size == "L"
    assert explicit.garment_size == "S"
    assert general.garment_size is None


async def test_the_migration_declares_the_head_it_revises(db):
    """A down_revision left on a placeholder is how a branch appears in the chain
    — this repo has a history of it (see docs/ALEMBIC_GUIDE.md)."""
    mod = _load()
    assert mod.revision == "20260927_kit_packet_scan"
    assert mod.down_revision == "20260923_material_arrival"


async def test_the_new_table_matches_the_model_column_for_column(db):
    """The migration and the model are two spellings of one table, and a
    divergence shows up as autogenerate drift on the next migration — which is
    the trap CLAUDE.md §11/§13 is about.
    """
    from app.modules.barcode.models import KitSubstitutionRequest
    mod = _load()
    # The migration's create_table call, read back off the module's own source.
    src = _MIGRATION.read_text(encoding="utf-8")
    model_cols = {c.name for c in KitSubstitutionRequest.__table__.columns}
    for name in model_cols:
        assert f'"{name}"' in src, (
            f"{name} is on the model but not in the migration — the live database "
            f"will not have it")
    assert mod  # the module imported cleanly, which is half the test
