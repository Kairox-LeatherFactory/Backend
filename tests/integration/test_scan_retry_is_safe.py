"""
================================================================================
tests/integration/test_scan_retry_is_safe.py — the scanner on bad wifi
================================================================================
A barcode terminal on factory wifi loses the response to a POST it has already
delivered, and retries. If the retry logs the work a second time, the garment
shows two events at one stage and — far worse — the leather is decremented
twice. That is stock disappearing because of a dropped packet.

THE AUDIT PLAN CALLED FOR AN `Idempotency-Key` HEADER HERE. It is not needed,
and these tests are why: `POST /production/log` is already idempotent on the
pair that actually identifies the work, (piece, stage). The service checks
whether the piece already has a valid event at the stage it is about to write
and, unless the DM has explicitly approved a redo, writes nothing and reports
the piece as already done.

That is a STRONGER guarantee than a request-scoped key. A key only protects a
literal replay of one request; this protects the same work arriving by any route
— a retry, a second operator scanning the same trolley, a manual entry after a
barcode entry. Adding a key on top would be a second mechanism guarding a
narrower case, with its own storage and expiry to get wrong.

So these tests exist instead of that feature. If they ever fail, the case for
the header is back.
================================================================================
"""
from datetime import date
from decimal import Decimal

import pytest

from app.core.enums import ScreenContext
from app.modules.materials.repository import MaterialRepository
from app.modules.materials.schemas import LotCreate
from app.modules.materials.service import MaterialService
from app.modules.production.service import ProductionService


async def _lot(db, qty=1000):
    out = await MaterialService(db).create_lot(LotCreate(
        category="LEATHER", article="RETRY-A", colour="BLACK",
        attributes={"thickness": "1.2mm", "dcm": qty}))
    await db.commit()
    return out["lot_id"]


@pytest.mark.asyncio
@pytest.mark.integrity
async def test_replaying_a_cut_scan_writes_nothing_and_spends_nothing(
        db, pieces, operations, cutter, cutting_mgr):
    """The same cut posted twice must cost the factory one cut's worth of hide."""
    lot_id = await _lot(db, 1000)
    repo = MaterialRepository(db)
    ids = [p.id for p, _d in pieces]

    first = await ProductionService(db).log_batch(
        user=cutting_mgr, employee_id=cutter[0].id, piece_ids=ids,
        work_date=date.today(), screen=ScreenContext.LEATHER_CUT,
        leather_lot_id=lot_id, consumption_qty=10.0)
    await db.commit()

    assert len(first["logged"]) == len(ids)
    after_first = (await repo.get_lot(lot_id)).on_hand
    assert after_first == Decimal("1000") - Decimal("10") * len(ids)

    # ── the terminal never saw the response, so it sends it again ────────────
    second = await ProductionService(db).log_batch(
        user=cutting_mgr, employee_id=cutter[0].id, piece_ids=ids,
        work_date=date.today(), screen=ScreenContext.LEATHER_CUT,
        leather_lot_id=lot_id, consumption_qty=10.0)
    await db.commit()

    assert second["logged"] == [], (
        "the replay logged the cut a second time — every piece now carries two "
        "events for one physical cut")
    assert len(second["rework"]) == len(ids), (
        "the replay should report each piece as already done")

    after_second = (await repo.get_lot(lot_id)).on_hand
    assert after_second == after_first, (
        f"the replay spent hide that was never cut: {after_first} -> "
        f"{after_second}. A dropped packet must not move the stock ledger.")


@pytest.mark.asyncio
@pytest.mark.integrity
async def test_a_partial_replay_still_logs_only_the_new_pieces(
        db, pieces, operations, cutter, cutting_mgr):
    """The realistic retry: the trolley grew between the two posts.

    An operator scans three pieces, the response is lost, two more garments go on
    the trolley and the whole lot is sent again. Only the two new ones are work.
    """
    lot_id = await _lot(db, 1000)
    repo = MaterialRepository(db)
    ids = [p.id for p, _d in pieces]
    assert len(ids) >= 5, "fixture must supply enough pieces for this split"

    await ProductionService(db).log_batch(
        user=cutting_mgr, employee_id=cutter[0].id, piece_ids=ids[:3],
        work_date=date.today(), screen=ScreenContext.LEATHER_CUT,
        leather_lot_id=lot_id, consumption_qty=10.0)
    await db.commit()
    after_first = (await repo.get_lot(lot_id)).on_hand

    second = await ProductionService(db).log_batch(
        user=cutting_mgr, employee_id=cutter[0].id, piece_ids=ids,
        work_date=date.today(), screen=ScreenContext.LEATHER_CUT,
        leather_lot_id=lot_id, consumption_qty=10.0)
    await db.commit()

    assert len(second["logged"]) == len(ids) - 3, (
        "only the garments that had not been cut yet are new work")
    assert len(second["rework"]) == 3

    after_second = (await repo.get_lot(lot_id)).on_hand
    spent = after_first - after_second
    assert spent == Decimal("10") * (len(ids) - 3), (
        f"charged {spent} dcm for {len(ids) - 3} new pieces — the three replayed "
        "pieces were charged again")
