"""
================================================================================
attendance/corrections.py — fixing a punch that named the wrong person
================================================================================
THE REAL MISTAKE, AND IT IS NOT "NOBODY WAS HERE"

    Security scans a card at the gate. The card said MAJID; the worker who
    actually did the day's cutting was SALIM. By the time anybody notices, MAJID
    has twelve cutting events against his name — and the wage for them.

    So the correction the floor needs is not "delete the attendance". It is
    RE-ALLOCATE IT TO THE RIGHT PERSON, and take the day's work with it. The
    work happened; it was simply filed under the wrong name, and the name is on
    both records.

WHY THE EVENTS MOVE IN THE SAME ACTION
    Leaving them behind would split one mistake into two jobs, and the second
    one is the one nobody remembers to do: attendance would say SALIM was here
    while the cutting log still paid MAJID. Wages are computed from the events,
    so the money would follow the wrong person no matter how many times the
    attendance was corrected.

    ONE ACTION, ONE REASON, ONE AUDIT ROW — and the response says exactly how
    many events moved, so the operator sees the size of what they just did.

DELETE IS NARROWER THAN EDIT
    Every operator (SECURITY, HR, MD, DM) may correct a punch: a gate operator
    who mis-scans should be able to fix it without going to find anybody.
    REMOVING the record decides whether somebody is paid for the day at all, so
    it stays with HR and management.

AND A CLOSED PAYROLL RUN IS A WALL, exactly as it is for production events. A
closed run is the document the cash was counted against and is never recomputed;
editing the attendance underneath it would leave the payroll disagreeing with the
days it claims to summarise.
================================================================================
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import RunStatus
from app.modules.attendance.models import AttendanceLog
from app.modules.production.models import ProductionEvent


class AttendanceCorrectionService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ══════════════════════════════════════════════════════════ guards
    async def _row(self, attendance_id: uuid.UUID) -> AttendanceLog:
        row = await self.db.get(AttendanceLog, attendance_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                "Attendance record not found.")
        return row

    async def _assert_payroll_open(self, row: AttendanceLog) -> None:
        """Refuse to touch a day a CLOSED payroll run has already been paid on.

        The same rule production events follow, and for the same reason: a closed
        run is never recomputed, so a change underneath it leaves the payroll
        disagreeing with the days it summarises.
        """
        from app.modules.wages.models import WageRun
        res = await self.db.execute(
            select(WageRun).where(WageRun.status == RunStatus.CLOSED,
                                  WageRun.period_start <= row.work_date,
                                  WageRun.period_end >= row.work_date))
        run = res.scalars().first()
        if run is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{row.work_date} falls inside a CLOSED payroll run "
                f"({run.period_start}..{run.period_end}) that has already been "
                f"paid. That run is a frozen snapshot and is never recomputed, "
                f"so the attendance underneath it cannot be changed — correct it "
                f"with a payroll adjustment instead.")

    async def _events_that_day(self, employee_id, work_date) -> list:
        res = await self.db.execute(
            select(ProductionEvent)
            .where(ProductionEvent.employee_id == employee_id,
                   ProductionEvent.work_date == work_date))
        return list(res.scalars().all())

    # ══════════════════════════════════════════════════════════ update
    async def update(self, attendance_id: uuid.UUID, *, employee_id=None,
                     check_in_at=None, check_out_at=None, reason=None,
                     actor_user_id=None, actor_name=None) -> dict:
        """Correct a punch. Changing the EMPLOYEE takes the day's work with it.

        THE RE-ALLOCATION IS THE POINT. `employee_id` is not "edit a field" — it
        is "this was the wrong person", and the twelve cutting events filed under
        that name are just as wrong as the punch. They move together or the
        money follows the wrong worker.
        """
        row = await self._row(attendance_id)
        await self._assert_payroll_open(row)

        before = {"employee_id": str(row.employee_id),
                  "check_in_at": row.check_in_at.isoformat() if row.check_in_at else None,
                  "check_out_at": row.check_out_at.isoformat() if row.check_out_at else None}
        moved_events = 0
        reallocated_from = None

        if employee_id is not None and employee_id != row.employee_id:
            from app.modules.employees.models import Employee
            target = await self.db.get(Employee, employee_id)
            if target is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND,
                                    "The employee to re-allocate to was not found.")
            # ONE PUNCH PER WORKER PER DAY is a database constraint, so
            # re-allocating onto somebody who was already marked present that day
            # would collide. Say so rather than letting the IntegrityError out.
            clash = await self.db.execute(
                select(AttendanceLog).where(
                    AttendanceLog.employee_id == employee_id,
                    AttendanceLog.work_date == row.work_date,
                    AttendanceLog.id != row.id))
            if clash.scalars().first() is not None:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"{target.name} is already marked present on "
                    f"{row.work_date}. Two attendance records for one worker on "
                    f"one day cannot both be right — delete the duplicate first.")

            reallocated_from = row.employee_id
            # THE DAY'S WORK MOVES WITH THE NAME.
            for ev in await self._events_that_day(row.employee_id, row.work_date):
                ev.employee_id = employee_id
                moved_events += 1
            row.employee_id = employee_id

        if check_in_at is not None:
            row.check_in_at = check_in_at
        if check_out_at is not None:
            row.check_out_at = check_out_at

        # Recompute the flags from the punches — a corrected time that left
        # is_short saying the old thing would be worse than not correcting it.
        if check_in_at is not None or check_out_at is not None:
            from app.modules.attendance.service import AttendanceService
            svc = AttendanceService(self.db)
            cfg = await svc._config()
            row.is_late, row.is_short, row.is_overtime = svc._flags(
                cfg, row.check_in_at, row.check_out_at)

        await self._audit(
            actor_user_id, "ATTENDANCE_CORRECTED", row.id,
            before=before,
            after={"employee_id": str(row.employee_id),
                   "check_in_at": row.check_in_at.isoformat() if row.check_in_at else None,
                   "check_out_at": row.check_out_at.isoformat() if row.check_out_at else None,
                   "reallocated_from": str(reallocated_from) if reallocated_from else None,
                   "production_events_moved": moved_events,
                   "reason": reason, "by": actor_name})
        await self.db.commit()
        await self.db.refresh(row)

        return {
            "attendance_id": row.id,
            "employee_id": row.employee_id,
            "work_date": row.work_date,
            "check_in_at": row.check_in_at,
            "check_out_at": row.check_out_at,
            "is_late": row.is_late, "is_short": row.is_short,
            "is_overtime": row.is_overtime,
            "production_events_moved": moved_events,
            "message": (
                f"Re-allocated to the correct worker; {moved_events} production "
                f"event(s) for {row.work_date} moved with it."
                if reallocated_from else "Attendance corrected."),
        }

    # ══════════════════════════════════════════════════════════ delete
    async def delete(self, attendance_id: uuid.UUID, *, reason: str,
                     actor_user_id=None, actor_name=None) -> dict:
        """Remove a punch that should never have been made.

        A REASON IS MANDATORY: deleting an attendance row decides whether
        somebody is paid for a day, and why it happened is the only thing that
        makes it reviewable afterwards.

        IF THE WORKER HAS WORK THAT DAY, THIS IS ALMOST CERTAINLY THE WRONG
        OPERATION. The usual cause of a wrong punch is a swapped card — the work
        really happened, it is filed under the wrong name — and the fix is to
        RE-ALLOCATE it, which moves the events too. Deleting instead leaves those
        events dated to a day the worker was never marked present, and the
        production log's own presence gate would have refused to create them.
        So the count is reported and the delete is refused, naming the better
        operation.
        """
        if not (reason or "").strip():
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Deleting an attendance record needs a reason — it decides "
                "whether somebody is paid for the day.")

        row = await self._row(attendance_id)
        await self._assert_payroll_open(row)

        events = await self._events_that_day(row.employee_id, row.work_date)
        if events:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"This worker has {len(events)} production event(s) on "
                f"{row.work_date}, so the work was really done — the usual cause "
                f"is a swapped card, not an absent worker. Re-allocate the "
                f"attendance to the right person instead (PATCH this record with "
                f"the correct employee_id); the events move with it. If the work "
                f"genuinely should not exist, delete those events first.")

        await self._audit(
            actor_user_id, "ATTENDANCE_DELETED", row.id,
            before={"employee_id": str(row.employee_id),
                    "work_date": str(row.work_date),
                    "check_in_at": row.check_in_at.isoformat() if row.check_in_at else None},
            after={"reason": reason, "by": actor_name})
        await self.db.delete(row)
        await self.db.commit()
        return {"attendance_id": attendance_id, "deleted": True,
                "message": "Attendance record removed."}

    async def _audit(self, actor_user_id, action, entity_id, *, before, after):
        """`actor_user_id` is an app_user.id — the LOGIN, never an employee.id."""
        from app.core.models import AuditLog
        self.db.add(AuditLog(
            actor_user_id=actor_user_id, action=action,
            entity_type="attendance_log", entity_id=entity_id,
            before=before, after=after, at=datetime.now(timezone.utc)))
