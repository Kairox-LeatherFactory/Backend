"""
INTEGRATION · what a cut has to tell us — bugs #9 and #10.

TWO RULES, AND THEY ARE DIFFERENT ON PURPOSE
    LEATHER is the expensive, per-hide-measured material the costing rests on, so
    a leather cut must still name a quantity and a lot. LINING is not: the client
    confirmed every field there (DCM, article, style, colour, thickness) is
    OPTIONAL, and a lining cut with nothing supplied is a complete record — the
    work happened, the piece advances, and no stock moves because nobody measured
    any. Demanding a number there only teaches the floor to invent one.

THE SPEC DOOR (bug #9)
    The cutting manager types article + colour (+ optional thickness) instead of
    picking a lot id. Thickness is FREE TEXT and optional: as a required dropdown
    it could not express a hide whose thickness was not already in the list, and
    the floor's workaround for that is to pick a wrong value.

AMBIGUITY IS AN ERROR. Two lots matching the typed spec is a 409 naming them, not
a silent pick of the first — a wrong lot decrements the one ledger the factory
reconciles against, invisibly.
"""
import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.core.enums import ScreenContext
from app.modules.barcode.models import MaterialLot
from app.modules.production.router import _resolve_cut_lot
from app.modules.production.schemas import Consumption
from app.modules.production.service import ProductionService

pytestmark = pytest.mark.integrity

TODAY = datetime.date.today()


async def _on_hand(db, lot_id) -> float:
    return float(await db.scalar(
        select(MaterialLot.on_hand).where(MaterialLot.id == lot_id)))


# ══════════════════════════════════════════════ bug #10 — lining is all-optional
@pytest.mark.asyncio
async def test_a_lining_cut_logs_with_no_consumption_at_all(
    db, operations, pieces, lining_cutter, lining_mgr
):
    piece, _ = pieces[0]

    res = await ProductionService(db).log_batch(
        user=lining_mgr, employee_id=lining_cutter[0].id, piece_ids=[piece.id],
        work_date=TODAY, screen=ScreenContext.LINING_CUT)

    assert res["count_logged"] == 1
    assert res["stage"] == "LINING_CUTTING"
    # No measurement was taken, so nothing is recorded and nothing is deducted.
    assert res["consumption_recorded"] is None
    assert res["stock_warning"] is None


@pytest.mark.asyncio
async def test_an_unmeasured_lining_cut_never_touches_stock(
    db, operations, pieces, lining_cutter, lining_mgr, leather_lot
):
    """THE MONEY PATH. A cut with no quantity must not invent one."""
    piece, _ = pieces[0]
    before = await _on_hand(db, leather_lot.id)

    await ProductionService(db).log_batch(
        user=lining_mgr, employee_id=lining_cutter[0].id, piece_ids=[piece.id],
        work_date=TODAY, screen=ScreenContext.LINING_CUT)

    assert await _on_hand(db, leather_lot.id) == before


@pytest.mark.asyncio
async def test_a_lining_cut_that_DOES_measure_still_decrements(
    db, operations, pieces, lining_cutter, lining_mgr, leather_lot
):
    """Optional is not ignored: supply a quantity and it behaves as before."""
    piece, _ = pieces[0]
    before = await _on_hand(db, leather_lot.id)

    res = await ProductionService(db).log_batch(
        user=lining_mgr, employee_id=lining_cutter[0].id, piece_ids=[piece.id],
        work_date=TODAY, screen=ScreenContext.LINING_CUT,
        lining_lot_id=leather_lot.id, consumption_qty=7.5)

    assert res["count_logged"] == 1
    assert res["consumption_recorded"]["qty"] == pytest.approx(7.5)
    assert await _on_hand(db, leather_lot.id) == pytest.approx(before - 7.5)


@pytest.mark.asyncio
async def test_a_leather_cut_still_demands_a_measurement(
    db, operations, pieces, cutter, cutting_mgr
):
    """The asymmetry is the point — leather keeps its requirement."""
    piece, _ = pieces[0]
    with pytest.raises(HTTPException) as exc:
        await ProductionService(db).log_batch(
            user=cutting_mgr, employee_id=cutter[0].id, piece_ids=[piece.id],
            work_date=TODAY, screen=ScreenContext.LEATHER_CUT)
    assert exc.value.status_code == 422
    assert "dcm" in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_a_measured_cut_with_no_material_named_is_rejected(
    db, operations, pieces, cutter, cutting_mgr
):
    piece, _ = pieces[0]
    with pytest.raises(HTTPException) as exc:
        await ProductionService(db).log_batch(
            user=cutting_mgr, employee_id=cutter[0].id, piece_ids=[piece.id],
            work_date=TODAY, screen=ScreenContext.LEATHER_CUT,
            consumption_qty=12.0)
    assert exc.value.status_code == 422
    assert "article" in str(exc.value.detail)


# ══════════════════════════════════════════════ bug #9 — the spec door
@pytest.mark.asyncio
async def test_article_and_colour_resolve_to_the_lot(db, leather_lot):
    got_leather, got_lining = await _resolve_cut_lot(
        db, Consumption(article="SUEDE-A32", colour="PINE GREEN", dcm=12.0),
        ScreenContext.LEATHER_CUT)
    assert got_leather == leather_lot.id and got_lining is None


@pytest.mark.asyncio
async def test_thickness_is_optional_and_free_text(db, leather_lot):
    """Supplying it narrows the match; omitting it is not an error; and it is a
    plain string, so a value that was never in any dropdown still works."""
    with_thickness, _ = await _resolve_cut_lot(
        db, Consumption(article="SUEDE-A32", colour="PINE GREEN",
                        thickness="1.2mm", dcm=12.0),
        ScreenContext.LEATHER_CUT)
    assert with_thickness == leather_lot.id

    without, _ = await _resolve_cut_lot(
        db, Consumption(article="SUEDE-A32", colour="PINE GREEN", dcm=12.0),
        ScreenContext.LEATHER_CUT)
    assert without == leather_lot.id


@pytest.mark.asyncio
async def test_an_unknown_article_is_404_and_says_what_to_do(db, leather_lot):
    with pytest.raises(HTTPException) as exc:
        await _resolve_cut_lot(
            db, Consumption(article="NO-SUCH-HIDE", colour="PINE GREEN", dcm=1),
            ScreenContext.LEATHER_CUT)
    assert exc.value.status_code == 404
    assert "POST /materials" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_an_ambiguous_spec_is_409_and_never_guesses(db, leather_lot):
    """Two lots of the same article+colour in different thicknesses. Picking one
    would silently decrement stock the manager never chose."""
    from app.modules.barcode.models import BarcodeRegistry
    from app.core.enums import BarcodeStatus, BarcodeType
    second = MaterialLot(category="LEATHER", article="SUEDE-A32",
                         colour="PINE GREEN", thickness="1.6mm", uom="dcm",
                         on_hand=500, is_active=True)
    db.add(second)
    await db.flush()
    db.add(BarcodeRegistry(code="LOT-LEA-000002",
                           type=BarcodeType.LEATHER_LOT.value,
                           status=BarcodeStatus.ACTIVE.value,
                           material_lot_id=second.id, caption="SUEDE-A32 1.6"))
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await _resolve_cut_lot(
            db, Consumption(article="SUEDE-A32", colour="PINE GREEN", dcm=12.0),
            ScreenContext.LEATHER_CUT)
    assert exc.value.status_code == 409
    detail = str(exc.value.detail)
    assert "2 LEATHER lots match" in detail
    assert "1.2mm" in detail and "1.6mm" in detail   # it NAMES the candidates

    # ...and adding the thickness disambiguates it.
    resolved, _ = await _resolve_cut_lot(
        db, Consumption(article="SUEDE-A32", colour="PINE GREEN",
                        thickness="1.6mm", dcm=12.0),
        ScreenContext.LEATHER_CUT)
    assert resolved == second.id


@pytest.mark.asyncio
async def test_an_explicit_lot_id_still_wins(db, leather_lot):
    """The id door is untouched — a screen that already picked a lot sends it."""
    got, _ = await _resolve_cut_lot(
        db, Consumption(leather_lot_id=leather_lot.id, dcm=12.0),
        ScreenContext.LEATHER_CUT)
    assert got == leather_lot.id


@pytest.mark.asyncio
async def test_a_lining_cut_naming_nothing_resolves_to_nothing(db, leather_lot):
    """Bug #10 again, at the resolver: no article means no lookup and no error."""
    assert await _resolve_cut_lot(db, Consumption(), ScreenContext.LINING_CUT) \
        == (None, None)
