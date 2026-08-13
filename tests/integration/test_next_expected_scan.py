"""
INTEGRATION · `/barcode/resolve` answers "what next?" for every barcode type.

THE BUG
    `next_expected_scan` handled DRAWER and PIECE codes and fell through to None
    for everything else. The production logger scans the EMPLOYEE card first, so
    the very first resolve of every workflow — the one the screen most needs to
    act on — came back null, and the frontend concluded the server did not know.

    It also reasoned only about drawers. A piece whose drawer already held both
    parts got null too, even though that piece plainly has a next production
    stage.

WHAT IS PINNED HERE
    1. Every barcode type gets an answer (the decision table).
    2. A piece also reports `next_stage`, derived from its completed operations.
    3. That stage is computed by the SAME pure helper the write path uses, so the
       screen can never be offered a stage the log would refuse — asserted
       directly against ProductionService in the last test.
"""
import datetime

import pytest

from app.core.enums import DrawerPart, ProductionStage, ScreenContext
from app.modules.barcode.service import BarcodeService
from app.modules.drawers.service import DrawerService
from app.modules.production.service import ProductionService

pytestmark = pytest.mark.integrity

TODAY = datetime.date.today()


# ══════════════════════════════════════════════ the decision table
@pytest.mark.asyncio
async def test_an_employee_card_asks_for_the_piece(db, cutter):
    """THE REGRESSION: this is the first scan of every workflow and it returned
    null."""
    emp, card = cutter
    out = await BarcodeService(db).resolve(card.code)
    assert out["type"] == "EMPLOYEE"
    assert out["next_expected_scan"] == "PIECE"


@pytest.mark.asyncio
async def test_a_lot_label_asks_for_the_piece(db, leather_lot):
    out = await BarcodeService(db).resolve("LOT-LEA-000001")
    assert out["next_expected_scan"] == "PIECE"


@pytest.mark.asyncio
async def test_a_drawer_holding_a_piece_asks_for_that_piece(db, pieces):
    _, drawer = pieces[0]
    out = await BarcodeService(db).resolve(drawer.code)
    assert out["type"] == "DRAWER"
    assert out["next_expected_scan"] == "PIECE"


@pytest.mark.asyncio
async def test_a_piece_whose_drawer_is_still_filling_asks_for_the_drawer(db, pieces):
    piece, _ = pieces[0]
    out = await BarcodeService(db).resolve(piece.code)
    assert out["next_expected_scan"] == "DRAWER"


@pytest.mark.asyncio
async def test_a_piece_whose_drawer_is_full_asks_for_no_further_scan(db, pieces):
    """Not a failure to answer — there IS no pairing scan left. The piece's next
    move is a production stage, which the same payload now reports."""
    piece, drawer = pieces[0]
    svc = DrawerService(db)
    for part in (DrawerPart.LEATHER, DrawerPart.LINING):
        await svc.store_scan(drawer_id=drawer.id, piece_id=piece.id, part=part)

    out = await BarcodeService(db).resolve(piece.code)
    assert out["next_expected_scan"] is None
    assert out["next_stage"] is not None, (
        "no pairing scan left is not the same as nothing to do — the stage must "
        "still be reported, or the screen sees a dead end")


# ══════════════════════════════════════════════ the production answer
@pytest.mark.asyncio
async def test_an_uncut_piece_reports_the_cut_and_where_it_is_logged(db, pieces):
    piece, _ = pieces[0]
    out = await BarcodeService(db).resolve(piece.code)
    assert out["next_stage"] == "LEATHER_CUTTING"
    assert out["next_stage_label"] == "Leather Cutting"
    assert "cut screen" in out["next_stage_blocked_reason"]


@pytest.mark.asyncio
async def test_the_stage_advances_with_the_piece(
    db, operations, pieces, cutter, cutting_mgr, leather_lot
):
    piece, _ = pieces[0]
    await ProductionService(db).log_batch(
        user=cutting_mgr, employee_id=cutter[0].id, piece_ids=[piece.id],
        work_date=TODAY, screen=ScreenContext.LEATHER_CUT,
        leather_lot_id=leather_lot.id, consumption_qty=12.0)

    out = await BarcodeService(db).resolve(piece.code)
    assert out["next_stage"] == "FUSING"
    assert out["next_stage_blocked_reason"] is None    # nothing holds it back


@pytest.mark.asyncio
async def test_the_merge_gate_is_reported_and_names_the_drawer(
    db, operations, pieces, cutter, paster, cutting_mgr, stitching_mgr, leather_lot
):
    """The piece is due at LINE_STITCHING but its drawer has not been sent. Both
    facts are returned: where it is going, and what is holding it."""
    piece, drawer = pieces[0]
    svc = ProductionService(db)
    await svc.log_batch(user=cutting_mgr, employee_id=cutter[0].id,
                        piece_ids=[piece.id], work_date=TODAY,
                        screen=ScreenContext.LEATHER_CUT,
                        leather_lot_id=leather_lot.id, consumption_qty=12.0)
    for _ in range(2):      # FUSING then PASTING
        await svc.log_batch(user=stitching_mgr, employee_id=paster[0].id,
                            piece_ids=[piece.id], work_date=TODAY,
                            screen=ScreenContext.PIPELINE)

    out = await BarcodeService(db).resolve(piece.code)
    assert out["next_stage"] == "LINE_STITCHING"
    assert drawer.code in out["next_stage_blocked_reason"]
    assert "Drawers List" in out["next_stage_blocked_reason"]


@pytest.mark.asyncio
async def test_a_finished_piece_says_so_instead_of_going_quiet(
    db, operations, pieces, cutter, tailor, cutting_mgr, dm, leather_lot
):
    piece, drawer = pieces[0]
    svc = ProductionService(db)
    await svc.log_batch(user=cutting_mgr, employee_id=cutter[0].id,
                        piece_ids=[piece.id], work_date=TODAY,
                        screen=ScreenContext.LEATHER_CUT,
                        leather_lot_id=leather_lot.id, consumption_qty=12.0)
    drawers = DrawerService(db)
    for part in (DrawerPart.LEATHER, DrawerPart.LINING):
        await drawers.store_scan(drawer_id=drawer.id, piece_id=piece.id, part=part)
    await drawers.send_batch(drawer_ids=[drawer.id], actor_id=dm.id)
    # Walk the rest of the chain until the server itself says there is nothing
    # left — a hard-coded count silently rots the moment a stage is added.
    for _ in range(len(ProductionStage.leather_chain()) + 1):
        res = await svc.log_batch(user=dm, employee_id=tailor[0].id,
                                  piece_ids=[piece.id], work_date=TODAY,
                                  screen=ScreenContext.PIPELINE)
        if res["count_logged"] == 0:
            break

    out = await BarcodeService(db).resolve(piece.code)
    assert out["next_stage"] is None
    assert "Finished" in out["next_stage_label"]


# ══════════════════════════════════════════════ read and write must agree
@pytest.mark.asyncio
async def test_resolve_and_the_log_infer_the_same_stage(
    db, operations, pieces, cutter, cutting_mgr, stitching_mgr, leather_lot
):
    """THE AGREEMENT TEST. `/resolve` advertises a stage; `/production/log` picks
    one. They read the same helper, and this is what keeps that true."""
    piece, _ = pieces[0]
    svc = ProductionService(db)
    await svc.log_batch(user=cutting_mgr, employee_id=cutter[0].id,
                        piece_ids=[piece.id], work_date=TODAY,
                        screen=ScreenContext.LEATHER_CUT,
                        leather_lot_id=leather_lot.id, consumption_qty=12.0)

    advertised = (await BarcodeService(db).resolve(piece.code))["next_stage"]
    inferred = await svc._infer_stage_for_piece(
        await svc.db.get(type(piece), piece.id), ScreenContext.PIPELINE)
    assert advertised == inferred.value
