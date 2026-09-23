"""
INTEGRATION · correcting a punch that named the wrong person.

THE REAL MISTAKE IS NOT "NOBODY WAS HERE". Security scans a card at the gate;
the card said MAJID, the worker who actually did the cutting was SALIM. By the
time anybody notices, MAJID has a day's events against his name — and the wage.

So the correction is RE-ALLOCATION, and the day's work moves with the name.
Leaving it behind splits one mistake into two jobs, and the second is the one
nobody remembers: attendance would say SALIM while the cutting log paid MAJID,
and wages are computed from the events.
"""
import datetime
import uuid

import pytest

from app.core.enums import RunStatus
from app.modules.attendance.corrections import AttendanceCorrectionService
from app.modules.attendance.models import AttendanceLog
from app.modules.production.models import ProductionEvent

pytestmark = pytest.mark.asyncio
TODAY = datetime.date.today()


def _piece(pieces, i=0):
    return pieces[i]


async def _punch(db, employee_id, work_date=None):
    from sqlalchemy import select
    wd = work_date or TODAY
    row = (await db.execute(
        select(AttendanceLog).where(AttendanceLog.employee_id == employee_id,
                                    AttendanceLog.work_date == wd))
    ).scalars().first()
    if row is None:
        row = AttendanceLog(
            id=uuid.uuid4(), employee_id=employee_id, work_date=wd,
            check_in_at=datetime.datetime.now(datetime.timezone.utc))
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


async def _work(db, operations, piece, employee_id, n=3):
    for code in list(operations)[:n]:
        db.add(ProductionEvent(
            sku_id=piece.sku_id, operation_id=operations[code].id,
            employee_id=employee_id, work_date=TODAY, qty=1, piece_id=piece.id,
            entered_by="test"))
    await db.commit()


# ══════════════════════════════════════════════════ the re-allocation
async def test_a_swapped_card_moves_the_whole_day_to_the_right_worker(
        db, operations, pieces, cutter, absent_worker):
    """THE CASE THIS EXISTS FOR.

    The work happened; it was filed under the wrong name, and the name is on both
    records. Moving only the punch would leave the cutting log paying the wrong
    person no matter how many times the attendance was corrected.
    """
    from sqlalchemy import func, select
    # `absent_worker` has no punch of their own — which is the real shape of
    # the story: SALIM was never scanned in, because MAJID's card was used.
    wrong, right = cutter[0], absent_worker[0]
    punch = await _punch(db, wrong.id)
    piece = _piece(pieces)
    await _work(db, operations, piece, wrong.id, n=3)

    out = await AttendanceCorrectionService(db).update(
        punch.id, employee_id=right.id, reason="card swapped at the gate")

    assert out["employee_id"] == right.id
    assert out["production_events_moved"] == 3
    assert "moved with it" in out["message"]

    theirs = await db.scalar(
        select(func.count()).select_from(ProductionEvent)
        .where(ProductionEvent.employee_id == right.id,
               ProductionEvent.work_date == TODAY))
    orphaned = await db.scalar(
        select(func.count()).select_from(ProductionEvent)
        .where(ProductionEvent.employee_id == wrong.id,
               ProductionEvent.work_date == TODAY))
    assert (theirs, orphaned) == (3, 0), "the day moved whole"


async def test_only_that_day_moves(db, operations, pieces, cutter, absent_worker):
    """A swapped card is one day's mistake. Yesterday's work stays put."""
    from sqlalchemy import func, select
    yesterday = TODAY - datetime.timedelta(days=1)
    piece = _piece(pieces)
    db.add(ProductionEvent(
        sku_id=piece.sku_id, operation_id=list(operations.values())[0].id,
        employee_id=cutter[0].id, work_date=yesterday, qty=1, piece_id=piece.id))
    await db.commit()
    await _work(db, operations, piece, cutter[0].id, n=2)

    punch = await _punch(db, cutter[0].id)
    out = await AttendanceCorrectionService(db).update(
        punch.id, employee_id=absent_worker[0].id, reason="wrong card")
    assert out["production_events_moved"] == 2

    still = await db.scalar(
        select(func.count()).select_from(ProductionEvent)
        .where(ProductionEvent.employee_id == cutter[0].id,
               ProductionEvent.work_date == yesterday))
    assert still == 1, "yesterday was a different day and a different mistake"


async def test_re_allocating_onto_someone_already_present_is_refused(
        db, operations, cutter, paster):
    """One punch per worker per day is a database constraint. Say so rather than
    letting the IntegrityError out as a 500."""
    from fastapi import HTTPException
    await _punch(db, paster[0].id)
    punch = await _punch(db, cutter[0].id)
    with pytest.raises(HTTPException) as exc:
        await AttendanceCorrectionService(db).update(
            punch.id, employee_id=paster[0].id)
    assert exc.value.status_code == 409
    assert "already marked present" in str(exc.value.detail)


async def test_correcting_a_time_recomputes_the_flags(db, cutter):
    """A corrected punch that left is_short saying the old thing would be worse
    than not correcting it at all."""
    punch = await _punch(db, cutter[0].id)
    start = datetime.datetime.now(datetime.timezone.utc).replace(
        hour=9, minute=0, second=0, microsecond=0)
    out = await AttendanceCorrectionService(db).update(
        punch.id, check_in_at=start,
        check_out_at=start + datetime.timedelta(hours=5))
    assert out["is_short"] is True, "five hours is short of an eight-hour shift"

    out2 = await AttendanceCorrectionService(db).update(
        punch.id, check_out_at=start + datetime.timedelta(hours=9))
    assert out2["is_short"] is False
    assert out2["is_overtime"] is True


async def test_the_correction_is_audited_with_both_names_and_the_count(
        db, operations, pieces, cutter, absent_worker):
    from sqlalchemy import select
    from app.core.models import AuditLog
    punch = await _punch(db, cutter[0].id)
    await _work(db, operations, _piece(pieces), cutter[0].id, n=2)
    await AttendanceCorrectionService(db).update(
        punch.id, employee_id=absent_worker[0].id, reason="card swapped",
        actor_name="SECURITY")

    log = (await db.execute(
        select(AuditLog).where(AuditLog.action == "ATTENDANCE_CORRECTED"))
    ).scalars().first()
    assert log.before["employee_id"] == str(cutter[0].id)
    assert log.after["employee_id"] == str(absent_worker[0].id)
    assert log.after["production_events_moved"] == 2
    assert log.after["reason"] == "card swapped"


# ══════════════════════════════════════════════════════════ deletion
async def test_a_punch_with_no_work_behind_it_can_be_deleted(db, cutter):
    """The simple case: marked present by mistake, nothing logged, remove it."""
    punch = await _punch(db, cutter[0].id)
    out = await AttendanceCorrectionService(db).delete(
        punch.id, reason="marked the wrong person at the gate")
    assert out["deleted"] is True
    assert await db.get(AttendanceLog, punch.id) is None


async def test_deleting_without_a_reason_is_refused(db, cutter):
    """It decides whether somebody is paid for the day."""
    from fastapi import HTTPException
    punch = await _punch(db, cutter[0].id)
    with pytest.raises(HTTPException) as exc:
        await AttendanceCorrectionService(db).delete(punch.id, reason="  ")
    assert exc.value.status_code == 422


async def test_deleting_a_punch_with_work_behind_it_points_at_re_allocation(
        db, operations, pieces, cutter):
    """DELETING IS ALMOST CERTAINLY THE WRONG OPERATION HERE.

    The work was really done, so the cause is a swapped card rather than an
    absent worker. Deleting would leave those events dated to a day the worker
    was never marked present — which the production log's own presence gate
    would have refused to create.
    """
    from fastapi import HTTPException
    punch = await _punch(db, cutter[0].id)
    await _work(db, operations, _piece(pieces), cutter[0].id, n=3)
    with pytest.raises(HTTPException) as exc:
        await AttendanceCorrectionService(db).delete(punch.id, reason="not here")
    assert exc.value.status_code == 409
    detail = str(exc.value.detail)
    assert "3 production event" in detail
    assert "Re-allocate" in detail, "the refusal must name the better operation"


# ═══════════════════════════════════════════════ the closed-payroll wall
async def test_a_day_a_closed_run_has_paid_cannot_be_corrected(db, cutter, paster):
    """The same rule production events follow. A closed run is the document the
    cash was counted against and is never recomputed."""
    from fastapi import HTTPException
    from app.modules.wages.models import WageRun
    punch = await _punch(db, cutter[0].id)
    db.add(WageRun(id=uuid.uuid4(), period_start=TODAY, period_end=TODAY,
                   status=RunStatus.CLOSED))
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await AttendanceCorrectionService(db).update(
            punch.id, employee_id=paster[0].id)
    assert exc.value.status_code == 409
    assert "CLOSED payroll run" in str(exc.value.detail)

    with pytest.raises(HTTPException) as exc2:
        await AttendanceCorrectionService(db).delete(punch.id, reason="x")
    assert exc2.value.status_code == 409


async def test_an_open_run_does_not_block_a_correction(db, cutter, absent_worker):
    """Nothing has been paid yet, so the recompute picks the correction up."""
    from app.modules.wages.models import WageRun
    punch = await _punch(db, cutter[0].id)
    db.add(WageRun(id=uuid.uuid4(), period_start=TODAY, period_end=TODAY,
                   status=RunStatus.OPEN))
    await db.commit()
    out = await AttendanceCorrectionService(db).update(
        punch.id, employee_id=absent_worker[0].id)
    assert out["employee_id"] == absent_worker[0].id
