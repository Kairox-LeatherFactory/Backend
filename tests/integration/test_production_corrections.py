"""
INTEGRATION · correcting a production record without touching the database.

THE REPORTED PROBLEM. "Manager Zahoor assigned a piece to the wrong employee
during cutting, so we had to delete the record directly from the database."

No audit row, no reason, no actor — and the wage that follows the record moved
with no trace of who moved it. The permission was what forced it: there was no
API at all.

TWO OPERATIONS, TWO DIFFERENT MISTAKES:
  REASSIGN  the work happened and the hide WAS cut; only the name is wrong.
  DELETE    the record should not exist; the stock has to come back.

AND A CLOSED PAYROLL RUN IS A WALL. Wage lines are COMPUTED from these events at
read time, so while a run is OPEN a correction just recomputes. Once it is
CLOSED it is the document the cash was counted against and is never recomputed —
editing underneath it would leave the payroll disagreeing with the events it
claims to summarise, with nothing to say which is right.
"""
import datetime
import uuid

import pytest

from app.core.enums import RunStatus
from app.modules.production.corrections import CorrectionService
from app.modules.production.models import ProductionEvent

pytestmark = pytest.mark.asyncio
TODAY = datetime.date.today()


def _piece(pieces, i=0):
    return pieces[i][0] if isinstance(pieces[i], tuple) else pieces[i]


async def _event(db, operations, piece, employee_id, code="LEATHER_CUTTING",
                 lot_id=None, qty=None):
    ev = ProductionEvent(
        sku_id=piece.sku_id, operation_id=operations[code].id,
        employee_id=employee_id, work_date=TODAY, qty=1, piece_id=piece.id,
        entered_by="test", leather_lot_id=lot_id, consumption_qty=qty)
    db.add(ev)
    piece.current_operation_id = operations[code].id
    await db.commit()
    await db.refresh(ev)
    return ev


async def _closed_run(db):
    from app.modules.wages.models import WageRun
    run = WageRun(id=uuid.uuid4(), period_start=TODAY, period_end=TODAY,
                  status=RunStatus.CLOSED)
    db.add(run)
    await db.commit()
    return run


# ═════════════════════════════════════════════════════════════ reassign
async def test_the_wrong_name_is_corrected_without_moving_stock(
        db, operations, pieces, cutter, paster, leather_lot):
    """The garment WAS cut and the hide IS gone. Only who did it was wrong.

    Returning the leather here would invent material that does not exist.
    """
    piece = _piece(pieces)
    ev = await _event(db, operations, piece, cutter[0].id,
                      lot_id=leather_lot.id, qty=12.0)
    before = float(leather_lot.on_hand)

    out = await CorrectionService(db).reassign(
        ev.id, employee_id=paster[0].id, reason="scanned the wrong card")
    assert out["employee_id"] == paster[0].id
    await db.refresh(leather_lot)
    assert float(leather_lot.on_hand) == before, "the hide was really cut"


async def test_reassigning_to_the_same_worker_is_not_an_error(
        db, operations, pieces, cutter):
    """A manager re-submitting the same correction should not be punished for
    checking whether the first one landed."""
    piece = _piece(pieces)
    ev = await _event(db, operations, piece, cutter[0].id)
    out = await CorrectionService(db).reassign(ev.id, employee_id=cutter[0].id)
    assert "already assigned" in (out["note"] or "")


async def test_a_reassign_is_audited_with_both_names(
        db, operations, pieces, cutter, paster):
    """The route this replaces was a database UPDATE: no actor, no before, no
    reason, and no way to find out afterwards that it had happened."""
    from sqlalchemy import select
    from app.core.models import AuditLog
    piece = _piece(pieces)
    ev = await _event(db, operations, piece, cutter[0].id)
    await CorrectionService(db).reassign(
        ev.id, employee_id=paster[0].id, reason="wrong card",
        actor_user_id=None, actor_name="ZAHOOR")

    log = (await db.execute(
        select(AuditLog).where(AuditLog.action == "PRODUCTION_EVENT_REASSIGNED"))
    ).scalars().first()
    assert log is not None
    assert log.before["employee_id"] == str(cutter[0].id)
    assert log.after["employee_id"] == str(paster[0].id)
    assert log.after["reason"] == "wrong card"


# ══════════════════════════════════════════════════════════════ delete
async def test_deleting_a_bogus_record_returns_its_stock(
        db, operations, pieces, cutter, leather_lot):
    """Nothing was cut, so the decrement that rode with it was fiction too."""
    piece = _piece(pieces)
    ev = await _event(db, operations, piece, cutter[0].id,
                      lot_id=leather_lot.id, qty=12.0)
    before = float(leather_lot.on_hand)

    out = await CorrectionService(db).delete(
        ev.id, reason="scanned the wrong piece")
    assert out["deleted"] is True
    assert out["stock_returned"] == 12.0
    await db.refresh(leather_lot)
    assert float(leather_lot.on_hand) == before + 12.0


async def test_a_deletion_without_a_reason_is_refused(
        db, operations, pieces, cutter):
    """This is the operation that erases evidence. Why it happened is the only
    thing that makes it reviewable, and the database route had nowhere to put
    one."""
    from fastapi import HTTPException
    piece = _piece(pieces)
    ev = await _event(db, operations, piece, cutter[0].id)
    with pytest.raises(HTTPException) as exc:
        await CorrectionService(db).delete(ev.id, reason="   ")
    assert exc.value.status_code == 422
    assert "reason" in str(exc.value.detail)


async def test_deleting_rewinds_the_garment_to_where_it_really_is(
        db, operations, pieces, cutter):
    """current_operation_id is what the screens read as where a garment is.

    Leaving it at a stage whose event has just been deleted shows the piece
    somewhere it has never been.
    """
    piece = _piece(pieces)
    await _event(db, operations, piece, cutter[0].id, code="LEATHER_CUTTING")
    second = await _event(db, operations, piece, cutter[0].id, code="FUSING")
    assert piece.current_operation_id == operations["FUSING"].id

    await CorrectionService(db).delete(second.id, reason="double tap")
    await db.refresh(piece)
    assert piece.current_operation_id == operations["LEATHER_CUTTING"].id


async def test_deleting_an_event_with_no_material_says_so(
        db, operations, pieces, cutter):
    piece = _piece(pieces)
    ev = await _event(db, operations, piece, cutter[0].id, code="FUSING")
    out = await CorrectionService(db).delete(ev.id, reason="test scan")
    assert out["stock_returned"] is None
    assert "No material" in out["message"]


# ════════════════════════════════════════════════ the closed-payroll wall
async def test_an_event_a_closed_run_has_paid_cannot_be_reassigned(
        db, operations, pieces, cutter, paster):
    """THE PROJECT'S OWN INVARIANT, enforced at this door.

    Wage lines are computed from these events, so editing one inside a closed
    period would leave the payroll disagreeing with the events it summarises —
    with nothing to say which is right. A closed run is never recomputed.
    """
    from fastapi import HTTPException
    piece = _piece(pieces)
    ev = await _event(db, operations, piece, cutter[0].id)
    await _closed_run(db)
    with pytest.raises(HTTPException) as exc:
        await CorrectionService(db).reassign(ev.id, employee_id=paster[0].id)
    assert exc.value.status_code == 409
    assert "CLOSED payroll run" in str(exc.value.detail)


async def test_an_event_a_closed_run_has_paid_cannot_be_deleted(
        db, operations, pieces, cutter):
    from fastapi import HTTPException
    piece = _piece(pieces)
    ev = await _event(db, operations, piece, cutter[0].id)
    await _closed_run(db)
    with pytest.raises(HTTPException) as exc:
        await CorrectionService(db).delete(ev.id, reason="wrong piece")
    assert exc.value.status_code == 409
    assert "frozen snapshot" in str(exc.value.detail)


async def test_an_open_run_does_not_block_a_correction(
        db, operations, pieces, cutter, paster):
    """While a run is OPEN nothing has been paid, so the recompute picks the
    correction up and no wage surgery is needed at all."""
    from app.modules.wages.models import WageRun
    piece = _piece(pieces)
    ev = await _event(db, operations, piece, cutter[0].id)
    db.add(WageRun(id=uuid.uuid4(), period_start=TODAY, period_end=TODAY,
                   status=RunStatus.OPEN))
    await db.commit()
    out = await CorrectionService(db).reassign(ev.id, employee_id=paster[0].id)
    assert out["employee_id"] == paster[0].id
