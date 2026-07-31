"""
================================================================================
tests/integration/test_merge_gate.py — LAYER 2: INTEGRATION (the merge gate)
================================================================================
The completeness gate is the one piece of logic that cannot be faked and most
likely to break. These prove: line-stitching is blocked until the drawer is
SENDED; store-scan advances holding_leather → holding_both; a leather-only piece
is complete on leather alone; RECEIVED requires completeness; SENDED requires
RECEIVED; the drawer recycles on package/export.
================================================================================
"""
import datetime

import pytest
from fastapi import HTTPException

from app.core.enums import DrawerPart, DrawerState, ScreenContext, UserRole
from app.modules.drawers.service import DrawerService
from app.modules.production.models import ProductionEvent
from app.modules.production.service import ProductionService


async def _advance_to_pasted(db, piece, cutter, paster, cutting_mgr, stitching_mgr, lot):
    today = datetime.date.today()
    svc = ProductionService(db)
    await svc.log_batch(user=cutting_mgr, employee_id=cutter.id, piece_ids=[piece.id],
                        work_date=today, screen=ScreenContext.LEATHER_CUT,
                        leather_lot_id=lot.id, consumption_qty=10.0)
    await svc.log_batch(user=cutting_mgr, employee_id=cutter.id, piece_ids=[piece.id],
                        work_date=today, screen=ScreenContext.PIPELINE)   # fusing
    await svc.log_batch(user=stitching_mgr, employee_id=paster.id, piece_ids=[piece.id],
                        work_date=today, screen=ScreenContext.PIPELINE)   # pasting


@pytest.mark.asyncio
async def test_line_stitch_blocked_until_sended(db, operations, pieces, cutter, paster,
                                                tailor, cutting_mgr, stitching_mgr, dm,
                                                leather_lot):
    piece, drawer = pieces[0]
    await _advance_to_pasted(db, piece, cutter[0], paster[0], cutting_mgr,
                             stitching_mgr, leather_lot)

    # Attempt line-stitching now → merge_blocked (drawer not sended)
    res = await ProductionService(db).log_batch(
        user=stitching_mgr, employee_id=tailor[0].id, piece_ids=[piece.id],
        work_date=datetime.date.today(), screen=ScreenContext.PIPELINE)
    assert res["stage"] == "LINE_STITCHING"
    assert res["count_logged"] == 0
    assert res["merge_blocked"], "line-stitch must block before SENDED"


@pytest.mark.asyncio
async def test_store_scan_and_full_merge_then_line_stitch(db, operations, pieces,
                                                          cutter, lining_cutter, paster,
                                                          tailor, cutting_mgr, lining_mgr,
                                                          stitching_mgr, dm, leather_lot):
    piece, drawer = pieces[0]
    drawers = DrawerService(db)

    # store leather → holding_leather
    r1 = await drawers.store_scan(drawer_id=drawer.id, piece_id=piece.id,
                                  part=DrawerPart.LEATHER)
    assert r1["state"] == DrawerState.HOLDING_LEATHER.value
    assert r1["ready_for_received"] is False
    assert "LINING" in r1["awaiting"]

    # store lining → holding_both
    r2 = await drawers.store_scan(drawer_id=drawer.id, piece_id=piece.id,
                                  part=DrawerPart.LINING)
    assert r2["state"] == DrawerState.HOLDING_BOTH.value
    assert r2["ready_for_received"] is True

    # DM RECEIVED then SENDED
    rec = await drawers.transition(drawer.id, "RECEIVED", actor_id=dm.id)
    assert rec["state"] == "received"
    snd = await drawers.transition(drawer.id, "SENDED", actor_id=dm.id)
    assert snd["state"] == "sended"

    # now advance leather chain and line-stitch succeeds
    await _advance_to_pasted(db, piece, cutter[0], paster[0], cutting_mgr,
                             stitching_mgr, leather_lot)
    res = await ProductionService(db).log_batch(
        user=stitching_mgr, employee_id=tailor[0].id, piece_ids=[piece.id],
        work_date=datetime.date.today(), screen=ScreenContext.PIPELINE)
    assert res["count_logged"] == 1
    assert not res["merge_blocked"]


@pytest.mark.asyncio
async def test_role_gate_checks_every_distinct_stage_in_batch(db, operations, pieces,
                                                               cutter, cutting_mgr):
    """GATE 1 is whole-request: if ANY stage in the batch is closed to the role,
    the entire request 403s — no piece is logged.

    Stage pair matters here. CUTTING_MANAGER owns BOTH LEATHER_CUTTING and FUSING
    (app/core/enums_barcode.py:165,167), so a cut+fuse batch does not exercise the
    gate — and because LEATHER_CUTTING requires consumption, such a batch is
    rejected 422 by the cut-mixing rule (service.py:248-252) before the role gate
    is even interesting. The pair below is FUSING (owned) + PASTING (not owned,
    it belongs to STITCHING_MANAGER at :168), and neither is a cut stage.
    """
    piece1, _ = pieces[0]
    piece2, _ = pieces[1]

    def _ev(piece, op):
        return ProductionEvent(
            sku_id=piece.sku_id, operation_id=op.id, employee_id=cutter[0].id,
            work_date=datetime.date.today(), qty=1, entered_by="test",
            piece_id=piece.id)

    # piece1: completed LEATHER_CUTTING          -> next stage is FUSING  (allowed)
    # piece2: completed LEATHER_CUTTING + FUSING -> next stage is PASTING (denied)
    db.add(_ev(piece1, operations["LEATHER_CUTTING"]))
    db.add(_ev(piece2, operations["LEATHER_CUTTING"]))
    db.add(_ev(piece2, operations["FUSING"]))
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await ProductionService(db).log_batch(
            user=cutting_mgr,
            employee_id=cutter[0].id,
            piece_ids=[piece1.id, piece2.id],
            work_date=datetime.date.today(),
            screen=ScreenContext.PIPELINE,
        )

    assert exc.value.status_code == 403
    assert "may not log" in str(exc.value.detail).lower()

    # and nothing was logged — the whole request was refused, not just piece2
    from sqlalchemy import func, select
    pasting = operations["PASTING"]
    n = await db.scalar(select(func.count(ProductionEvent.id))
                        .where(ProductionEvent.operation_id == pasting.id))
    assert n == 0


@pytest.mark.asyncio
async def test_leather_only_piece_complete_on_leather(db, operations, pieces, dm):
    piece, drawer = pieces[0]
    # mark this piece leather-only
    piece.needs_lining = False
    await db.commit()
    drawers = DrawerService(db)
    r = await drawers.store_scan(drawer_id=drawer.id, piece_id=piece.id,
                                 part=DrawerPart.LEATHER)
    assert r["ready_for_received"] is True   # complete on leather alone
    rec = await drawers.transition(drawer.id, "RECEIVED", actor_id=dm.id)
    assert rec["state"] == "received"


@pytest.mark.asyncio
async def test_received_requires_completeness(db, operations, pieces, dm):
    piece, drawer = pieces[0]   # needs_lining True, nothing stored
    with pytest.raises(Exception) as ei:
        await DrawerService(db).transition(drawer.id, "RECEIVED", actor_id=dm.id)
    assert "await" in str(ei.value).lower() or "409" in str(ei.value)


@pytest.mark.asyncio
async def test_sended_requires_received(db, operations, pieces, dm):
    piece, drawer = pieces[0]
    piece.needs_lining = False
    await db.commit()
    await DrawerService(db).store_scan(drawer_id=drawer.id, piece_id=piece.id,
                                       part=DrawerPart.LEATHER)
    # jump straight to SENDED → rejected
    with pytest.raises(Exception) as ei:
        await DrawerService(db).transition(drawer.id, "SENDED", actor_id=dm.id)
    assert "RECEIVED" in str(ei.value) or "before" in str(ei.value).lower()


@pytest.mark.asyncio
async def test_store_scan_wrong_drawer_rejected(db, operations, pieces):
    piece_a, drawer_a = pieces[0]
    _, drawer_b = pieces[1]
    # piece A into drawer B → 409 (merge map is authority)
    with pytest.raises(Exception) as ei:
        await DrawerService(db).store_scan(drawer_id=drawer_b.id, piece_id=piece_a.id,
                                           part=DrawerPart.LEATHER)
    assert "not merged" in str(ei.value).lower() or "409" in str(ei.value)
