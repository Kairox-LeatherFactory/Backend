"""
INTEGRATION · garments sent to an outside factory, and what they cost.

THE GAP. When a deadline is short, tailoring (or cutting, or lining) goes out to
another factory. Nothing modelled that: the pieces stopped moving, nobody could
say where they were, and the money paid for the work was recorded nowhere.

THE TWO RULES THE FACTORY CHOSE:
  · a garment that is OUT cannot be scanned in-house. Otherwise the system
    records line-stitching done here on a jacket sitting twenty miles away.
  · the vendor's work IS the stage, logged against the vendor rather than an
    employee — the garment must advance and the counts must include it, but
    nobody on our payroll did it so no wage may be generated.

AND THE MONEY: "even when we give it to the outside factory we are paying for
each piece, so we have to track that." Only pieces that came BACK are paid for.
"""
import datetime
import uuid

import pytest
import pytest_asyncio

from app.core.enums import ProductionStage, ScreenContext
from app.modules.jobwork.service import JobWorkService
from app.modules.production.models import ProductionEvent
from app.modules.production.service import ProductionService

pytestmark = pytest.mark.asyncio
TODAY = datetime.date.today()


def _piece(pieces, i=0):
    return pieces[i][0] if isinstance(pieces[i], tuple) else pieces[i]


async def _ready_for_stitching(db, operations, piece, emp_id):
    """A garment through the cut side and released from the store — the state a
    dispatch for LINE_STITCHING is made from."""
    for code in ("LEATHER_CUTTING", "FUSING", "PASTING"):
        db.add(ProductionEvent(
            sku_id=piece.sku_id, operation_id=operations[code].id,
            employee_id=emp_id, work_date=TODAY, qty=1, piece_id=piece.id))
    piece.store_state = "sended"
    piece.leather_in = True
    piece.lining_in = True
    piece.needs_lining = False
    await db.commit()


@pytest_asyncio.fixture
async def vendor(db):
    return await JobWorkService(db).create_vendor(
        name="ABC Tailors", contact="98000 00000")


# ══════════════════════════════════════════════════════════════ vendors
async def test_registering_the_same_vendor_twice_returns_the_first(db):
    """A duplicate is not a mistake worth blocking with a 409."""
    svc = JobWorkService(db)
    a = await svc.create_vendor(name="ABC Tailors")
    b = await svc.create_vendor(name="  abc tailors  ")
    assert a["vendor_id"] == b["vendor_id"]


# ══════════════════════════════════════════════════════════════ dispatch
async def test_dispatching_takes_the_garments_out_of_the_building(
        db, operations, pieces, cutter, vendor):
    piece = _piece(pieces)
    await _ready_for_stitching(db, operations, piece, cutter[0].id)
    job = await JobWorkService(db).dispatch(
        vendor_id=vendor["vendor_id"], stage="LINE_STITCHING",
        piece_ids=[piece.id], rate_per_piece=45.0, currency="INR")
    assert job["pieces_out"] == 1
    assert job["status"] == "OUT"
    assert job["vendor"] == "ABC Tailors"


async def test_a_garment_cannot_be_at_two_vendors_at_once(
        db, operations, pieces, cutter, vendor):
    """Two open records claiming one piece would each look satisfied by the
    other's return."""
    from fastapi import HTTPException
    piece = _piece(pieces)
    await _ready_for_stitching(db, operations, piece, cutter[0].id)
    svc = JobWorkService(db)
    await svc.dispatch(vendor_id=vendor["vendor_id"], stage="LINE_STITCHING",
                       piece_ids=[piece.id])
    other = await svc.create_vendor(name="XYZ Stitching")
    with pytest.raises(HTTPException) as exc:
        await svc.dispatch(vendor_id=other["vendor_id"],
                           stage="LINE_STITCHING", piece_ids=[piece.id])
    assert exc.value.status_code == 409
    assert "already out at ABC Tailors" in str(exc.value.detail)


async def test_one_unavailable_garment_does_not_lose_the_rest_of_the_dispatch(
        db, operations, pieces, cutter, vendor):
    piece_a, piece_b = _piece(pieces, 0), _piece(pieces, 1)
    for p in (piece_a, piece_b):
        await _ready_for_stitching(db, operations, p, cutter[0].id)
    svc = JobWorkService(db)
    await svc.dispatch(vendor_id=vendor["vendor_id"], stage="LINE_STITCHING",
                       piece_ids=[piece_a.id])
    other = await svc.create_vendor(name="XYZ Stitching")
    job = await svc.dispatch(vendor_id=other["vendor_id"],
                             stage="LINE_STITCHING",
                             piece_ids=[piece_a.id, piece_b.id])
    assert job["pieces_out"] == 1
    assert len(job["skipped"]) == 1


async def test_an_overdue_job_says_so(db, operations, pieces, cutter, vendor):
    """A dispatch nobody chases is how garments go missing."""
    piece = _piece(pieces)
    await _ready_for_stitching(db, operations, piece, cutter[0].id)
    job = await JobWorkService(db).dispatch(
        vendor_id=vendor["vendor_id"], stage="LINE_STITCHING",
        piece_ids=[piece.id],
        expected_back=TODAY - datetime.timedelta(days=2))
    assert job["overdue"] is True
    late = await JobWorkService(db).list_jobs(overdue_only=True)
    assert [j["job_id"] for j in late] == [job["job_id"]]


# ═══════════════════════════════════════════════════════════════ GATE 7
async def test_a_garment_at_a_vendor_cannot_be_scanned_in_house(
        db, operations, pieces, cutter, tailor, dm, vendor):
    """Otherwise the system records line-stitching done here on a jacket sitting
    at another factory, and nothing would ever contradict it."""
    piece = _piece(pieces)
    await _ready_for_stitching(db, operations, piece, cutter[0].id)
    await JobWorkService(db).dispatch(
        vendor_id=vendor["vendor_id"], stage="LINE_STITCHING",
        piece_ids=[piece.id])

    res = await ProductionService(db).log_batch(
        user=dm, employee_id=tailor[0].id, piece_ids=[piece.id],
        work_date=TODAY, screen=ScreenContext.PIPELINE)
    assert res["logged"] == []
    assert res["offsite_blocked"] == [piece.code]
    reason = next(b["reason"] for b in res["blocked"] if b["gate"] == "offsite")
    assert "ABC Tailors" in reason


async def test_one_garment_at_a_vendor_does_not_lose_the_tray(
        db, operations, pieces, cutter, tailor, dm, vendor):
    away, here = _piece(pieces, 0), _piece(pieces, 1)
    for p in (away, here):
        await _ready_for_stitching(db, operations, p, cutter[0].id)
    await JobWorkService(db).dispatch(
        vendor_id=vendor["vendor_id"], stage="LINE_STITCHING",
        piece_ids=[away.id])

    res = await ProductionService(db).log_batch(
        user=dm, employee_id=tailor[0].id, piece_ids=[away.id, here.id],
        work_date=TODAY, screen=ScreenContext.PIPELINE)
    assert res["offsite_blocked"] == [away.code]
    assert here.code in res["logged"]


# ══════════════════════════════════════════════════════════════ return
async def test_the_return_logs_the_stage_against_the_vendor_not_a_worker(
        db, operations, pieces, cutter, vendor):
    """The work really happened, so the garment advances and the counts include
    it — but nobody on our payroll did it, so no wage may be generated.

    `employee_id` is NULL and the wage query inner-joins Employee, so the event
    drops out of payroll on its own.
    """
    from sqlalchemy import select
    piece = _piece(pieces)
    await _ready_for_stitching(db, operations, piece, cutter[0].id)
    svc = JobWorkService(db)
    job = await svc.dispatch(vendor_id=vendor["vendor_id"],
                             stage="LINE_STITCHING", piece_ids=[piece.id])
    await svc.receive(job["job_id"])

    ev = (await db.execute(
        select(ProductionEvent)
        .where(ProductionEvent.piece_id == piece.id,
               ProductionEvent.operation_id == operations["LINE_STITCHING"].id))
    ).scalars().first()
    assert ev is not None, "the stage the vendor performed must be recorded"
    assert ev.employee_id is None, "nobody on our payroll earned this"
    assert ev.vendor_id == vendor["vendor_id"]


async def test_a_returned_garment_can_be_scanned_again(
        db, operations, pieces, cutter, tailor, dm, vendor):
    """The block is about being away, not about having been away."""
    piece = _piece(pieces)
    await _ready_for_stitching(db, operations, piece, cutter[0].id)
    svc = JobWorkService(db)
    job = await svc.dispatch(vendor_id=vendor["vendor_id"],
                             stage="LINE_STITCHING", piece_ids=[piece.id])
    await svc.receive(job["job_id"])

    res = await ProductionService(db).log_batch(
        user=dm, employee_id=tailor[0].id, piece_ids=[piece.id],
        work_date=TODAY, screen=ScreenContext.PIPELINE)
    assert res["offsite_blocked"] == []
    assert res["stage"] == "SHELL_STITCHING", "it advanced past what the vendor did"


async def test_only_the_pieces_that_came_back_are_paid_for(
        db, operations, pieces, cutter, vendor):
    """"We are paying for each piece" — but not for one that never returned, and
    not for one that came back badly done.

    SHORT and REJECTED are kept apart because they lead to different
    conversations: a missing garment is a loss to chase with the vendor, a bad
    one is a quality matter. Neither is work delivered.
    """
    a, b, c = _piece(pieces, 0), _piece(pieces, 1), _piece(pieces, 2)
    for p in (a, b, c):
        await _ready_for_stitching(db, operations, p, cutter[0].id)
    svc = JobWorkService(db)
    job = await svc.dispatch(
        vendor_id=vendor["vendor_id"], stage="LINE_STITCHING",
        piece_ids=[a.id, b.id, c.id], rate_per_piece=45.0, currency="INR")

    out = await svc.receive(job["job_id"], rejected_ids=[c.id])
    assert out["pieces_back"] == 2
    assert out["pieces_rejected"] == 1
    assert out["cost"] == 90.0, "45 x 2 returned; the rejected one is not paid"


async def test_a_job_with_no_rate_reports_no_cost_rather_than_zero(
        db, operations, pieces, cutter, vendor):
    """Some work is settled another way. Reporting 0 would read as free."""
    piece = _piece(pieces)
    await _ready_for_stitching(db, operations, piece, cutter[0].id)
    svc = JobWorkService(db)
    job = await svc.dispatch(vendor_id=vendor["vendor_id"],
                             stage="LINE_STITCHING", piece_ids=[piece.id])
    out = await svc.receive(job["job_id"])
    assert out["cost"] is None


async def test_a_partial_return_is_reported_as_partial(
        db, operations, pieces, cutter, vendor):
    """"The job is back" is a different statement from "every piece is back"."""
    a, b = _piece(pieces, 0), _piece(pieces, 1)
    for p in (a, b):
        await _ready_for_stitching(db, operations, p, cutter[0].id)
    svc = JobWorkService(db)
    job = await svc.dispatch(vendor_id=vendor["vendor_id"],
                             stage="LINE_STITCHING", piece_ids=[a.id, b.id])
    out = await svc.receive(job["job_id"], piece_ids=[a.id])
    assert out["status"] == "PARTIAL"
    assert out["pieces_out"] == 1
    # And the one still out is still blocked in-house.
    assert (await svc.current_job_for_piece(b.id)) is not None
    assert (await svc.current_job_for_piece(a.id)) is None


async def test_receiving_twice_does_not_log_the_stage_twice(
        db, operations, pieces, cutter, vendor):
    """A re-tap of the return must not write a second event."""
    from sqlalchemy import func, select
    piece = _piece(pieces)
    await _ready_for_stitching(db, operations, piece, cutter[0].id)
    svc = JobWorkService(db)
    job = await svc.dispatch(vendor_id=vendor["vendor_id"],
                             stage="LINE_STITCHING", piece_ids=[piece.id])
    await svc.receive(job["job_id"])
    await svc.receive(job["job_id"])

    n = await db.scalar(
        select(func.count()).select_from(ProductionEvent)
        .where(ProductionEvent.piece_id == piece.id,
               ProductionEvent.operation_id == operations["LINE_STITCHING"].id))
    assert n == 1
