"""
INTEGRATION · cutting consumption and the stock decrement — one of the deepest
paths the harness calls out (conftest.py:12-14).

WHAT MAKES THIS DELICATE
    Two facts must move together or the books are wrong:
      1. N production events (the act of cutting N pieces), and
      2. ONE stock decrement of qty x N against the lot.

    They live in the SAME transaction (production/service.py:346-355 then
    :355 commit) because a hide that was cut but not deducted overstates stock,
    and a deduction with no event understates output. B4 added the second half of
    the rule: a REWORK pass re-logs the event but consumes no new hide, so it
    must not move stock at all.

`test_merge_gate.py` drives cutting to reach later stages; nothing yet asserts
the arithmetic. That is this file.
"""
import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.core.enums import ScreenContext
from app.modules.barcode.models import MaterialLot
from app.modules.production.models import ProductionEvent
from app.modules.production.service import ProductionService

pytestmark = pytest.mark.integrity

TODAY = datetime.date.today()


async def _on_hand(db, lot_id) -> float:
    return float(await db.scalar(
        select(MaterialLot.on_hand).where(MaterialLot.id == lot_id)))


# ══════════════════════════════════════════════════════════════ happy path
@pytest.mark.asyncio
async def test_cutting_one_piece_decrements_the_lot_once(db, operations, pieces,
                                                         cutter, cutting_mgr,
                                                         leather_lot):
    piece, _ = pieces[0]
    before = await _on_hand(db, leather_lot.id)

    res = await ProductionService(db).log_batch(
        user=cutting_mgr, employee_id=cutter[0].id, piece_ids=[piece.id],
        work_date=TODAY, screen=ScreenContext.LEATHER_CUT,
        leather_lot_id=leather_lot.id, consumption_qty=10.0)

    assert res["count_logged"] == 1
    assert await _on_hand(db, leather_lot.id) == pytest.approx(before - 10.0)
    assert res["consumption_recorded"]["dcm"] == pytest.approx(10.0)
    assert res["consumption_recorded"]["pieces_consuming"] == 1


@pytest.mark.asyncio
async def test_a_batch_decrements_once_for_the_whole_batch_not_once_per_query(
        db, operations, pieces, cutter, cutting_mgr, leather_lot):
    """B4 (service.py:346-353): `total = consumption_qty * fresh_cut`, applied in
    a single call. 5 pieces at 10 dcm is one 50 dcm move, not five races."""
    ids = [p.id for p, _ in pieces]
    before = await _on_hand(db, leather_lot.id)

    res = await ProductionService(db).log_batch(
        user=cutting_mgr, employee_id=cutter[0].id, piece_ids=ids,
        work_date=TODAY, screen=ScreenContext.LEATHER_CUT,
        leather_lot_id=leather_lot.id, consumption_qty=10.0)

    assert res["count_logged"] == 5
    assert res["consumption_recorded"]["pieces_consuming"] == 5
    assert res["consumption_recorded"]["dcm"] == pytest.approx(50.0)
    assert await _on_hand(db, leather_lot.id) == pytest.approx(before - 50.0)


@pytest.mark.asyncio
async def test_the_consumption_rides_the_event_never_the_piece(
        db, operations, pieces, cutter, cutting_mgr, leather_lot):
    """CLAUDE.md §8: 'The lot link lives on the EVENT (the act of cutting), never
    on the piece.' A piece cut twice from two lots has two truths; a column on
    the piece could only hold one."""
    piece, _ = pieces[0]
    await ProductionService(db).log_batch(
        user=cutting_mgr, employee_id=cutter[0].id, piece_ids=[piece.id],
        work_date=TODAY, screen=ScreenContext.LEATHER_CUT,
        leather_lot_id=leather_lot.id, consumption_qty=12.5)

    ev = (await db.execute(
        select(ProductionEvent).where(ProductionEvent.piece_id == piece.id))
    ).scalars().first()
    assert ev.leather_lot_id == leather_lot.id
    assert float(ev.consumption_qty) == pytest.approx(12.5)
    assert ev.lining_lot_id is None


# ═══════════════════════════════════════════ common variation: rework is free
@pytest.mark.asyncio
async def test_a_rework_cut_re_logs_the_event_but_consumes_no_new_hide(
        db, operations, pieces, cutter, cutting_mgr, leather_lot):
    """B4's second half (service.py:317-318, :346). `fresh_cut` counts only
    pieces cutting for the FIRST time. Re-scanning a piece that was already cut
    is a rework pass over material already deducted — deducting again would
    invent consumption that never happened."""
    piece, _ = pieces[0]
    svc = ProductionService(db)
    await svc.log_batch(
        user=cutting_mgr, employee_id=cutter[0].id, piece_ids=[piece.id],
        work_date=TODAY, screen=ScreenContext.LEATHER_CUT,
        leather_lot_id=leather_lot.id, consumption_qty=10.0)
    after_first = await _on_hand(db, leather_lot.id)

    res = await ProductionService(db).log_batch(
        user=cutting_mgr, employee_id=cutter[0].id, piece_ids=[piece.id],
        work_date=TODAY, screen=ScreenContext.LEATHER_CUT,
        leather_lot_id=leather_lot.id, consumption_qty=10.0)

    assert res["rework"] == [piece.code], "the second pass must be flagged as rework"
    assert res["consumption_recorded"] is None, "a rework pass reported consumption"
    assert await _on_hand(db, leather_lot.id) == pytest.approx(after_first), (
        "stock moved on a rework pass — material was deducted twice for one cut")


@pytest.mark.asyncio
async def test_a_mixed_batch_charges_only_the_pieces_cutting_for_the_first_time(
        db, operations, pieces, cutter, cutting_mgr, leather_lot):
    """The realistic floor case: a manager rescans a tray holding one already-cut
    piece alongside four fresh ones. Only the four may move stock."""
    already, _ = pieces[0]
    svc = ProductionService(db)
    await svc.log_batch(
        user=cutting_mgr, employee_id=cutter[0].id, piece_ids=[already.id],
        work_date=TODAY, screen=ScreenContext.LEATHER_CUT,
        leather_lot_id=leather_lot.id, consumption_qty=10.0)
    before = await _on_hand(db, leather_lot.id)

    res = await ProductionService(db).log_batch(
        user=cutting_mgr, employee_id=cutter[0].id,
        piece_ids=[p.id for p, _ in pieces], work_date=TODAY,
        screen=ScreenContext.LEATHER_CUT,
        leather_lot_id=leather_lot.id, consumption_qty=10.0)

    assert res["consumption_recorded"]["pieces_consuming"] == 4
    assert await _on_hand(db, leather_lot.id) == pytest.approx(before - 40.0)


@pytest.mark.asyncio
async def test_a_duplicated_piece_id_in_one_request_is_charged_once(
        db, operations, pieces, cutter, cutting_mgr, leather_lot):
    """A double-scan of the same barcode inside one batch. `seen` de-duplicates
    (service.py:294-299) — otherwise a jittery scanner gun doubles the hide
    consumption of whatever it double-read."""
    piece, _ = pieces[0]
    before = await _on_hand(db, leather_lot.id)

    res = await ProductionService(db).log_batch(
        user=cutting_mgr, employee_id=cutter[0].id,
        piece_ids=[piece.id, piece.id, piece.id], work_date=TODAY,
        screen=ScreenContext.LEATHER_CUT,
        leather_lot_id=leather_lot.id, consumption_qty=10.0)

    assert res["count_logged"] == 1
    assert await _on_hand(db, leather_lot.id) == pytest.approx(before - 10.0)


# ════════════════════════════════════════════════════════ error-throwing paths
@pytest.mark.asyncio
async def test_cutting_without_a_quantity_is_refused(db, operations, pieces,
                                                     cutter, cutting_mgr,
                                                     leather_lot):
    piece, _ = pieces[0]
    with pytest.raises(HTTPException) as exc:
        await ProductionService(db).log_batch(
            user=cutting_mgr, employee_id=cutter[0].id, piece_ids=[piece.id],
            work_date=TODAY, screen=ScreenContext.LEATHER_CUT,
            leather_lot_id=leather_lot.id, consumption_qty=None)
    assert exc.value.status_code == 422
    assert "consumption" in str(exc.value.detail).lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("qty", [0, -1, -0.5])
async def test_a_non_positive_quantity_is_refused(db, operations, pieces, cutter,
                                                  cutting_mgr, leather_lot, qty):
    """A zero or negative cut is not a cut. Allowing it would let a scan ADD
    stock back to the lot (service.py:280-283)."""
    piece, _ = pieces[0]
    with pytest.raises(HTTPException) as exc:
        await ProductionService(db).log_batch(
            user=cutting_mgr, employee_id=cutter[0].id, piece_ids=[piece.id],
            work_date=TODAY, screen=ScreenContext.LEATHER_CUT,
            leather_lot_id=leather_lot.id, consumption_qty=qty)
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_cutting_without_a_lot_is_refused(db, operations, pieces, cutter,
                                                cutting_mgr):
    """Consumption with no lot cannot be attributed to any material
    (service.py:284-290)."""
    piece, _ = pieces[0]
    with pytest.raises(HTTPException) as exc:
        await ProductionService(db).log_batch(
            user=cutting_mgr, employee_id=cutter[0].id, piece_ids=[piece.id],
            work_date=TODAY, screen=ScreenContext.LEATHER_CUT,
            leather_lot_id=None, consumption_qty=10.0)
    assert exc.value.status_code == 422
    assert "lot" in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_an_unknown_lot_is_a_404_and_nothing_is_logged(
        db, operations, pieces, cutter, cutting_mgr):
    """The decrement runs AFTER the events are staged (service.py:332-353) but
    BEFORE the single commit at :355. A bad lot must therefore take the whole
    batch down with it — events for a cut that consumed nothing are a phantom."""
    import uuid
    piece, _ = pieces[0]
    # Hold the id as a plain value: the rollback below expires every ORM
    # instance, and touching an expired attribute afterwards triggers a lazy
    # reload that cannot run inside the async session's greenlet. Same trap the
    # wage money-path suite documents (test_wage_run_money_path.py:107-109).
    piece_id = piece.id

    with pytest.raises(HTTPException) as exc:
        await ProductionService(db).log_batch(
            user=cutting_mgr, employee_id=cutter[0].id, piece_ids=[piece_id],
            work_date=TODAY, screen=ScreenContext.LEATHER_CUT,
            leather_lot_id=uuid.uuid4(), consumption_qty=10.0)
    assert exc.value.status_code == 404

    await db.rollback()
    n = await db.scalar(select(func.count(ProductionEvent.id))
                        .where(ProductionEvent.piece_id == piece_id))
    assert n == 0, "events survived a failed consumption — stock and output disagree"


@pytest.mark.asyncio
async def test_a_cut_may_not_be_mixed_with_other_stages_in_one_batch(
        db, operations, pieces, cutter, cutting_mgr, leather_lot):
    """service.py:248-252. Consumption is one number for the request; if the
    batch spanned CUTTING and FUSING there is no honest way to say which pieces
    the hide went into.

    Reaching the mixed state needs pieces at different points on the chain, so
    one piece is advanced past cutting first.
    """
    svc = ProductionService(db)
    advanced, _ = pieces[0]
    fresh, _ = pieces[1]
    await svc.log_batch(
        user=cutting_mgr, employee_id=cutter[0].id, piece_ids=[advanced.id],
        work_date=TODAY, screen=ScreenContext.LEATHER_CUT,
        leather_lot_id=leather_lot.id, consumption_qty=10.0)

    # PIPELINE now infers FUSING for `advanced` and LEATHER_CUTTING for `fresh`.
    with pytest.raises(HTTPException) as exc:
        await ProductionService(db).log_batch(
            user=cutting_mgr, employee_id=cutter[0].id,
            piece_ids=[advanced.id, fresh.id], work_date=TODAY,
            screen=ScreenContext.PIPELINE,
            leather_lot_id=leather_lot.id, consumption_qty=10.0)
    assert exc.value.status_code == 422
    assert "cutting scan may not be mixed" in str(exc.value.detail).lower()


# ═══════════════════════════════════════════════════════ crash / integrity
@pytest.mark.asyncio
async def test_cutting_more_than_the_lot_holds_drives_stock_negative(
        db, operations, pieces, cutter, cutting_mgr, leather_lot):
    """REPRODUCES PRIOR FINDING F14 (still OPEN — delta-register.md:60,
    pass-12-boundary-values.md:34-36, pass-06 for the atomic-UPDATE fix).

    `decrement_for_cut_nocommit` (materials/service.py:252-271) validates that
    qty > 0 and that the lot exists, but never compares the request against what
    the lot actually holds. `lot.on_hand = on_hand - d` (:269) is unguarded —
    no floor check, no `CHECK (on_hand >= 0)` in the migration, no row lock — so
    a fat-fingered consumption figure silently writes a NEGATIVE on_hand.

    Why that matters beyond a wrong number: `stock()` derives
    `available = on_hand - reserved` (materials/service.py:184) and
    `available_for_lot` does the same (:155). A negative on_hand therefore
    propagates into the DM's shortfall calculation and the supplier-order
    suggestion, so the factory under-orders against stock it does not have.

    No new finding is opened. This is the first EXECUTABLE reproduction of F14 —
    the prior passes cite it by inspection — so the fix now has a test that
    turns red the moment the floor check lands.
    """
    piece, _ = pieces[0]
    before = await _on_hand(db, leather_lot.id)   # fixture seeds 1000 dcm

    res = await ProductionService(db).log_batch(
        user=cutting_mgr, employee_id=cutter[0].id, piece_ids=[piece.id],
        work_date=TODAY, screen=ScreenContext.LEATHER_CUT,
        leather_lot_id=leather_lot.id, consumption_qty=before + 500)

    assert res["count_logged"] == 1
    assert await _on_hand(db, leather_lot.id) < 0, (
        "expected the unguarded negative recorded in F148")
    assert res["consumption_recorded"]["available_after"] < 0


@pytest.mark.asyncio
async def test_a_lining_cut_charges_the_lining_lot_and_leaves_leather_alone(
        db, operations, pieces, lining_cutter, lining_mgr, leather_lot):
    """The parallel cut path. LINING_CUTTING must read `lining_lot_id`
    (service.py:284-286); charging the leather lot for a lining cut would drain
    the wrong material and hide the real one."""
    from app.modules.barcode.models import MaterialLot as ML
    lining = ML(category="LINING", subtype="PLAIN_LINING", article="KNIT-01",
                colour="BLACK", thickness="0.5mm", uom="mtrs", on_hand=500,
                is_active=True)
    db.add(lining)
    await db.commit()
    await db.refresh(lining)

    piece, _ = pieces[0]
    leather_before = await _on_hand(db, leather_lot.id)

    res = await ProductionService(db).log_batch(
        user=lining_mgr, employee_id=lining_cutter[0].id, piece_ids=[piece.id],
        work_date=TODAY, screen=ScreenContext.LINING_CUT,
        lining_lot_id=lining.id, consumption_qty=4.0)

    assert res["count_logged"] == 1
    assert await _on_hand(db, lining.id) == pytest.approx(496.0)
    assert await _on_hand(db, leather_lot.id) == pytest.approx(leather_before), (
        "a lining cut moved leather stock")

    ev = (await db.execute(
        select(ProductionEvent).where(ProductionEvent.piece_id == piece.id))
    ).scalars().first()
    assert ev.lining_lot_id == lining.id and ev.leather_lot_id is None
