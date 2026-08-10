"""
INTEGRATION · the two-door log: stage inference, the four gates, consumption.

WHAT THIS FILE DEFENDS (CLAUDE.md §8)
    The caller NEVER sends a stage. A cut screen fixes it; PIPELINE infers it
    from each piece's own history. Then four gates run, cheapest first:

      1 ROLE      403 for the WHOLE request — the role is wrong for the batch
      2 SKILL     a recorded WARNING, never a block (the floor must not lose a
                  scan because HR has not backfilled a designation)
      3 SEQUENCE  per-piece: no skipping a chain stage
      4 MERGE     per-piece, LINE_STITCHING only: the drawer must be SENDED

    Gates 2-4 are per-piece precisely so ONE bad piece never loses the good ones
    a manager scanned with it — that is asserted directly below, not implied.

    Cut stages additionally capture consumption per piece and decrement the lot
    ONCE per batch; PACKAGE_EXPORT recycles the drawer.
"""
import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.core.enums import DrawerPart, DrawerState, ScreenContext
from app.modules.drawers.service import DrawerService
from app.modules.production.models import ProductionEvent
from app.modules.production.service import ProductionService

pytestmark = pytest.mark.integrity

TODAY = datetime.date.today()


async def _log(db, user, emp, piece_ids, *, screen=ScreenContext.PIPELINE, **kw):
    # the employee fixtures yield (employee, barcode) — accept either form
    employee = emp[0] if isinstance(emp, tuple) else emp
    return await ProductionService(db).log_batch(
        user=user, employee_id=employee.id, piece_ids=piece_ids,
        work_date=TODAY, screen=screen, **kw)


async def _cut(db, cutting_mgr, cutter, piece, lot, qty=10.0):
    return await _log(db, cutting_mgr, cutter, [piece.id],
                      screen=ScreenContext.LEATHER_CUT,
                      leather_lot_id=lot.id, consumption_qty=qty)


# ══════════════════════════════════════════════════ stage inference
@pytest.mark.asyncio
async def test_the_cut_screen_fixes_the_stage(db, operations, pieces, cutter,
                                              cutting_mgr, leather_lot):
    piece, _ = pieces[0]
    res = await _cut(db, cutting_mgr, cutter, piece, leather_lot)
    assert res["stage"] == "LEATHER_CUTTING"
    assert res["count_logged"] == 1 and res["logged"] == [piece.code]


@pytest.mark.asyncio
async def test_pipeline_infers_the_next_stage_from_the_pieces_own_history(
    db, operations, pieces, cutter, cutting_mgr, leather_lot
):
    """Cut → the next PIPELINE scan is FUSING, not 'whatever was sent'."""
    piece, _ = pieces[0]
    await _cut(db, cutting_mgr, cutter, piece, leather_lot)

    res = await _log(db, cutting_mgr, cutter, [piece.id])
    assert res["stage"] == "FUSING" and res["count_logged"] == 1


@pytest.mark.asyncio
async def test_a_second_scan_at_the_same_stage_is_rework_not_a_duplicate(
    db, operations, pieces, cutter, cutting_mgr, leather_lot
):
    piece, _ = pieces[0]
    await _cut(db, cutting_mgr, cutter, piece, leather_lot)
    again = await _cut(db, cutting_mgr, cutter, piece, leather_lot)

    assert again["count_logged"] == 0
    assert again["rework"] == [piece.code]
    events = await db.scalar(
        select(func.count(ProductionEvent.id)).where(
            ProductionEvent.piece_id == piece.id))
    assert events == 1                      # rework never writes a second row


# ══════════════════════════════════════════════════ GATE 1 — role (whole request)
@pytest.mark.asyncio
async def test_the_role_gate_403s_the_whole_request(
    db, operations, pieces, cutter, stitching_mgr, leather_lot
):
    piece, _ = pieces[0]
    with pytest.raises(HTTPException) as exc:
        await _log(db, stitching_mgr, cutter, [piece.id],
                   screen=ScreenContext.LEATHER_CUT,
                   leather_lot_id=leather_lot.id, consumption_qty=10.0)
    assert exc.value.status_code == 403
    assert await db.scalar(select(func.count(ProductionEvent.id))) == 0


@pytest.mark.asyncio
async def test_the_cutting_manager_owns_fusing(
    db, operations, pieces, cutter, cutting_mgr, leather_lot
):
    """CLAUDE.md §3: the cutting manager logs leather cutting AND fusing."""
    piece, _ = pieces[0]
    await _cut(db, cutting_mgr, cutter, piece, leather_lot)
    res = await _log(db, cutting_mgr, cutter, [piece.id])
    assert res["stage"] == "FUSING" and res["count_logged"] == 1


@pytest.mark.asyncio
async def test_md_and_dm_bypass_the_role_gate(db, operations, pieces, cutter,
                                              md, leather_lot):
    piece, _ = pieces[0]
    res = await _log(db, md, cutter, [piece.id], screen=ScreenContext.LEATHER_CUT,
                     leather_lot_id=leather_lot.id, consumption_qty=10.0)
    assert res["count_logged"] == 1


# ══════════════════════════════════════════════════ GATE 2 — skill (warning)
@pytest.mark.asyncio
async def test_a_wrong_skill_warns_but_still_logs(
    db, operations, pieces, paster, cutting_mgr, leather_lot
):
    piece, _ = pieces[0]
    res = await _log(db, cutting_mgr, paster, [piece.id],
                     screen=ScreenContext.LEATHER_CUT,
                     leather_lot_id=leather_lot.id, consumption_qty=10.0)
    assert res["count_logged"] == 1
    assert res["skill_blocked"] == []
    assert res["skill_warnings"][0]["designation"] == "PASTER"
    assert res["skill_warnings"][0]["stage"] == "LEATHER_CUTTING"


# ══════════════════════════════════════════════════ GATE 3 — sequence (per-piece)
@pytest.mark.asyncio
async def test_one_out_of_sequence_piece_does_not_lose_the_good_ones(
    db, operations, pieces, cutter, cutting_mgr, leather_lot
):
    """THE per-piece promise: piece 1 is cut and ready for FUSING; piece 2 was
    never cut. One batch → piece 1 logs, piece 2 lands in sequence_blocked."""
    ready, _ = pieces[0]
    skipped, _ = pieces[1]
    await _cut(db, cutting_mgr, cutter, ready, leather_lot)

    res = await _log(db, cutting_mgr, cutter, [ready.id, skipped.id])
    assert res["stage"] == "FUSING"
    assert res["logged"] == [ready.code]
    assert res["sequence_blocked"] == [skipped.code]
    assert res["count_logged"] == 1


# ══════════════════════════════════════════════════ GATE 4 — merge (per-piece)
@pytest.mark.asyncio
async def test_line_stitching_is_blocked_until_the_drawer_is_sended(
    db, operations, pieces, cutter, paster, cutting_mgr, stitching_mgr,
    leather_lot
):
    piece, drawer = pieces[0]
    await _cut(db, cutting_mgr, cutter, piece, leather_lot)
    await _log(db, cutting_mgr, cutter, [piece.id])            # FUSING
    await _log(db, stitching_mgr, paster, [piece.id])          # PASTING

    blocked = await _log(db, stitching_mgr, paster, [piece.id])
    assert blocked["stage"] == "LINE_STITCHING"
    assert blocked["merge_blocked"] == [piece.code]
    assert blocked["count_logged"] == 0

    drawers = DrawerService(db)
    await drawers.store_scan(drawer_id=drawer.id, piece_id=piece.id,
                             part=DrawerPart.LEATHER)
    await drawers.store_scan(drawer_id=drawer.id, piece_id=piece.id,
                             part=DrawerPart.LINING)
    await drawers.transition(drawer.id, "RECEIVED", actor_id=None)
    await drawers.transition(drawer.id, "SENDED", actor_id=None)

    released = await _log(db, stitching_mgr, paster, [piece.id])
    assert released["stage"] == "LINE_STITCHING"
    assert released["count_logged"] == 1


# ══════════════════════════════════════════════════ consumption
@pytest.mark.asyncio
async def test_stock_is_decremented_once_per_batch_not_once_per_piece(
    db, operations, pieces, cutter, cutting_mgr, leather_lot
):
    before = float(leather_lot.on_hand)
    ids = [p.id for p, _ in pieces[:3]]
    res = await _log(db, cutting_mgr, cutter, ids,
                     screen=ScreenContext.LEATHER_CUT,
                     leather_lot_id=leather_lot.id, consumption_qty=15.0)

    assert res["count_logged"] == 3
    rec = res["consumption_recorded"]
    assert rec["pieces_consuming"] == 3 and rec["qty"] == 45.0
    await db.refresh(leather_lot)
    assert float(leather_lot.on_hand) == before - 45.0
    assert rec["available_after"] == before - 45.0

    # the lot link + per-piece consumption live on the EVENT, never on the piece
    ev = (await db.execute(
        select(ProductionEvent).where(ProductionEvent.piece_id == ids[0])
    )).scalar_one()
    assert ev.leather_lot_id == leather_lot.id
    assert float(ev.consumption_qty) == 15.0


@pytest.mark.asyncio
async def test_a_cut_without_consumption_or_a_lot_is_422(
    db, operations, pieces, cutter, cutting_mgr, leather_lot
):
    piece, _ = pieces[0]
    with pytest.raises(HTTPException) as exc:
        await _log(db, cutting_mgr, cutter, [piece.id],
                   screen=ScreenContext.LEATHER_CUT, leather_lot_id=leather_lot.id)
    assert exc.value.status_code == 422

    with pytest.raises(HTTPException) as exc:
        await _log(db, cutting_mgr, cutter, [piece.id],
                   screen=ScreenContext.LEATHER_CUT, consumption_qty=10.0)
    assert exc.value.status_code == 422

    with pytest.raises(HTTPException) as exc:
        await _log(db, cutting_mgr, cutter, [piece.id],
                   screen=ScreenContext.LEATHER_CUT,
                   leather_lot_id=leather_lot.id, consumption_qty=0)
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_rework_at_a_cut_stage_does_not_double_charge_the_lot(
    db, operations, pieces, cutter, cutting_mgr, leather_lot
):
    """fresh_cut_count drives the decrement, so a rework scan consumes nothing."""
    piece, _ = pieces[0]
    await _cut(db, cutting_mgr, cutter, piece, leather_lot, qty=15.0)
    await db.refresh(leather_lot)
    after_first = float(leather_lot.on_hand)

    res = await _cut(db, cutting_mgr, cutter, piece, leather_lot, qty=15.0)
    assert res["rework"] == [piece.code]
    await db.refresh(leather_lot)
    assert float(leather_lot.on_hand) == after_first


# ══════════════════════════════════════════════════ misc gates + buckets
@pytest.mark.asyncio
async def test_an_absent_worker_cannot_be_logged(
    db, operations, pieces, absent_worker, cutting_mgr, leather_lot
):
    piece, _ = pieces[0]
    with pytest.raises(HTTPException) as exc:
        await _log(db, cutting_mgr, absent_worker[0], [piece.id],
                   screen=ScreenContext.LEATHER_CUT,
                   leather_lot_id=leather_lot.id, consumption_qty=10.0)
    assert exc.value.status_code == 400
    assert "not checked in" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_unknown_pieces_are_bucketed_not_fatal(
    db, operations, pieces, cutter, cutting_mgr, leather_lot
):
    import uuid as _uuid
    piece, _ = pieces[0]
    ghost = _uuid.uuid4()
    res = await _log(db, cutting_mgr, cutter, [piece.id, ghost],
                     screen=ScreenContext.LEATHER_CUT,
                     leather_lot_id=leather_lot.id, consumption_qty=10.0)
    assert res["logged"] == [piece.code]
    assert res["not_found"] == [str(ghost)]


@pytest.mark.asyncio
async def test_an_empty_batch_is_400(db, operations, cutter, cutting_mgr):
    with pytest.raises(HTTPException) as exc:
        await _log(db, cutting_mgr, cutter, [])
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_package_export_recycles_the_drawer(
    db, operations, pieces, cutter, paster, tailor, cutting_mgr, stitching_mgr,
    md, leather_lot
):
    """The ONLY point a drawer frees: the piece has shipped."""
    piece, drawer = pieces[0]
    drawers = DrawerService(db)

    await _cut(db, cutting_mgr, cutter, piece, leather_lot)
    await _log(db, cutting_mgr, cutter, [piece.id])             # FUSING
    await _log(db, stitching_mgr, paster, [piece.id])           # PASTING
    await drawers.store_scan(drawer_id=drawer.id, piece_id=piece.id,
                             part=DrawerPart.LEATHER)
    await drawers.store_scan(drawer_id=drawer.id, piece_id=piece.id,
                             part=DrawerPart.LINING)
    await drawers.transition(drawer.id, "RECEIVED", actor_id=None)
    await drawers.transition(drawer.id, "SENDED", actor_id=None)

    for expected in ("LINE_STITCHING", "SHELL_STITCHING", "FINAL_FINISH",
                     "FINAL_INSPECTION", "PACKAGE_EXPORT"):
        res = await _log(db, md, tailor, [piece.id])
        assert res["stage"] == expected and res["count_logged"] == 1

    await db.refresh(drawer)
    assert drawer.state == DrawerState.WAITING.value
    assert drawer.current_piece_id is None


# ══════════════════════════════════════════════════ reads
@pytest.mark.asyncio
async def test_the_piece_checklist_returns_its_envelope(
    db, operations, pieces, order_tree, cutter, cutting_mgr, leather_lot
):
    """GET /production/skus/{id}/pieces — the STORE-overlay edit dropped this
    function's `return` and mis-unpacked its 4-tuple rows, so the endpoint
    answered `null` (and 500'd on the unpack). The envelope is the contract."""
    piece, drawer = pieces[0]
    await _cut(db, cutting_mgr, cutter, piece, leather_lot)
    await DrawerService(db).store_scan(drawer_id=drawer.id, piece_id=piece.id,
                                       part=DrawerPart.LEATHER)

    out = await ProductionService(db).list_pieces_for_sku(
        sku_id=order_tree["sku"].id)

    assert out["sku_code"] == "JP-CLERMONT-PINE-M"
    assert out["total"] == 5 and out["done"] == 0 and out["pending"] == 5
    assert out["order_id"] == order_tree["order"].id
    assert len(out["pieces"]) == 5

    first = out["pieces"][0]
    assert first["code"] == piece.code and first["seq"] == 1
    assert first["event_stage"] == "LEATHER_CUTTING"    # the real event
    assert first["current_stage"] == "STORE"            # what the UI shows
    assert first["in_store"] is True
    assert first["store_status"] == DrawerState.HOLDING_LEATHER.value
    assert "awaiting lining" in first["current_stage_label"]


@pytest.mark.asyncio
async def test_the_checklist_marks_eligibility_against_an_operation(
    db, operations, pieces, order_tree, cutter, cutting_mgr, leather_lot
):
    done_piece, _ = pieces[0]
    await _cut(db, cutting_mgr, cutter, done_piece, leather_lot)

    out = await ProductionService(db).list_pieces_for_sku(
        sku_id=order_tree["sku"].id, operation_id=operations["FUSING"].id)

    assert out["operation_code"] == "FUSING"
    by_code = {p["code"]: p for p in out["pieces"]}
    assert by_code[done_piece.code]["eligible"] is True
    other = [p for c, p in by_code.items() if c != done_piece.code][0]
    assert other["eligible"] is False
    assert other["blocked_reason"] == "LEATHER_CUTTING not completed"
    assert out["blocked"] == 4


@pytest.mark.asyncio
async def test_style_progress_404s_for_another_clients_style(
    db, operations, pieces, order_tree, cutter, cutting_mgr, leather_lot
):
    """Tenancy: a scoped WHERE returning {} told a CLIENT the id was real.
    Existence itself is information — invisible or unknown is a 404."""
    import uuid as _uuid

    style_id = order_tree["style"].id
    await _cut(db, cutting_mgr, cutter, pieces[0][0], leather_lot)
    svc = ProductionService(db)

    assert (await svc.style_progress(style_id))["LEATHER_CUTTING"] == 1

    with pytest.raises(HTTPException) as exc:
        await svc.style_progress(style_id, client_scope=_uuid.uuid4())
    assert exc.value.status_code == 404

    with pytest.raises(HTTPException) as exc:
        await svc.style_progress(_uuid.uuid4())
    assert exc.value.status_code == 404
