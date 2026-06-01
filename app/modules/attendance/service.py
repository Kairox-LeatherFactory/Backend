"""
================================================================================
modules/attendance/service.py — Attendance business rules (async)
================================================================================

WHAT THIS FILE OWNS
  - Geofence validation BEFORE any database write (the spec's hard rule).
  - Server-side timestamps (block client-side clock manipulation — spec).
  - Late / short / overtime flags computed at write time (so dashboards never
    recompute on read).
  - Self-service check-in/out (Flow A) and supervisor proxy check-in (Flow B).
  - Supervisor onboarding of a daily-wage worker (Flow C).
  - The production-link helper used by the production module: is THIS worker
    actually checked in TODAY? (See "Why this matters for production" below.)
  - Daily-wage payroll: hours worked * daily_rate, summed over the period.

WHY THIS MATTERS FOR PRODUCTION
  A piece-rate worker who hasn't checked in cannot have legitimately produced
  pieces today. We use is_present_today() from the production service to BLOCK
  obvious fraud / data-entry mistakes (logging output for a worker who isn't on
  the floor). The flag also feeds the analytics: if 5 cutters checked in but
  CUTTING throughput is low, your bottleneck isn't headcount — it's process.
================================================================================
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import UserRole, WageType
from app.modules.attendance import schemas
from app.modules.attendance.geofence import within_geofence
from app.modules.attendance.models import (
    AttendanceLog, AttendanceSource, ShiftConfig,
)
from app.modules.attendance.repository import AttendanceRepository
from app.modules.employees.models import Employee
from app.modules.employees.service import EmployeeService
from app.modules.users.models import User


class AttendanceService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = AttendanceRepository(db)
        self.employees = EmployeeService(db)

    # ══════════════════════════════════════════════════════════════════
    # Internal helpers
    # ══════════════════════════════════════════════════════════════════
    async def _config(self) -> ShiftConfig:
        return await self.repo.get_config()

    async def _enforce_geofence(self, lat: float, lon: float) -> float:
        """Spec: distance > radius -> block with a clear error."""
        cfg = await self._config()
        ok, dist = within_geofence(lat, lon, float(cfg.factory_lat),
                                   float(cfg.factory_lon), cfg.radius_m)
        if not ok:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"You must be within {cfg.radius_m} meters of the factory "
                f"(you are {dist:.0f} m away).",
            )
        return dist

    def _now(self) -> datetime:
        """SERVER timestamp (spec: 'blocking client-side time manipulation')."""
        return datetime.now(timezone.utc)

    def _flags(self, cfg: ShiftConfig, check_in: datetime,
               check_out: datetime | None) -> tuple[bool, bool, bool]:
        """Compute (is_late, is_short, is_overtime) from policy + punches."""
        # Late: check-in past (shift_start + grace).
        h, m = (int(x) for x in cfg.shift_start.split(":"))
        shift_start_today = check_in.replace(hour=h, minute=m, second=0, microsecond=0)
        grace = timedelta(minutes=cfg.late_grace_minutes)
        is_late = check_in > (shift_start_today + grace)

        is_short = is_ot = False
        if check_out:
            worked_h = (check_out - check_in).total_seconds() / 3600.0
            std = float(cfg.shift_length_hours)
            is_short = worked_h < std
            is_ot = worked_h > std
        return is_late, is_short, is_ot

    # ══════════════════════════════════════════════════════════════════
    # Flow A — Self-service check-in / check-out
    # ══════════════════════════════════════════════════════════════════
    async def self_check_in(self, user: User, body: schemas.CheckInRequest) -> AttendanceLog:
        """The acting user IS the worker (manager / HR / permanent worker)."""
        if user.employee_id is None:
            raise HTTPException(400, "This login is not linked to an employee record.")
        dist = await self._enforce_geofence(body.lat, body.lon)
        return await self._open_or_reject(
            employee_id=user.employee_id, source=AttendanceSource.SELF,
            recorded_by=None, distance_m=dist,
        )

    async def self_check_out(self, user: User, body: schemas.CheckOutRequest) -> AttendanceLog:
        if user.employee_id is None:
            raise HTTPException(400, "This login is not linked to an employee record.")
        await self._enforce_geofence(body.lat, body.lon)
        return await self._close(employee_id=user.employee_id)

    # ══════════════════════════════════════════════════════════════════
    # Flow B — Supervisor proxy-marks daily-wage workers
    # ══════════════════════════════════════════════════════════════════
    async def proxy_mark_present(self, supervisor: User,
                                 body: schemas.ProxyMarkRequest) -> list[AttendanceLog]:
        # Permission: SUPERVISOR (and DIRECT_MANAGER as superuser) only.
        if supervisor.role not in (UserRole.SUPERVISOR, UserRole.DIRECT_MANAGER):
            raise HTTPException(403, "Only a supervisor may proxy-mark attendance.")
        dist = await self._enforce_geofence(body.lat, body.lon)

        out: list[AttendanceLog] = []
        for emp_id in body.employee_ids:
            emp = await self.employees.get(emp_id)
            if not emp:
                continue                                    # silently skip unknown ids
            # Spec restricts PROXY to daily-wage workers.
            if emp.wage_type != WageType.DAILY_WAGE:
                raise HTTPException(
                    400, f"{emp.name} is not a daily-wage worker — proxy not allowed.")
            log = await self._open_or_reject(
                employee_id=emp.id, source=AttendanceSource.PROXY,
                recorded_by=supervisor.id, distance_m=dist,
            )
            out.append(log)
        return out

    async def proxy_check_out(self, supervisor: User,
                              body: schemas.ProxyMarkRequest) -> list[AttendanceLog]:
        if supervisor.role not in (UserRole.SUPERVISOR, UserRole.DIRECT_MANAGER):
            raise HTTPException(403, "Only a supervisor may proxy check-out.")
        await self._enforce_geofence(body.lat, body.lon)
        out = []
        for emp_id in body.employee_ids:
            out.append(await self._close(employee_id=emp_id))
        return out

    # ══════════════════════════════════════════════════════════════════
    # Flow C — Onboard a new daily-wage worker on the floor
    # ══════════════════════════════════════════════════════════════════
    async def add_daily_worker(self, supervisor: User,
                               body: schemas.AddDailyWorkerRequest) -> Employee:
        if supervisor.role not in (UserRole.SUPERVISOR, UserRole.DIRECT_MANAGER):
            raise HTTPException(403, "Only a supervisor may add daily workers.")
        return await self.employees.create(
            name=body.name, designation=body.designation,
            wage_type=WageType.DAILY_WAGE,
            daily_rate=body.daily_rate, phone=body.phone, email=None,
        )

    # ══════════════════════════════════════════════════════════════════
    # Open / close primitives (used by both flows)
    # ══════════════════════════════════════════════════════════════════
    async def _open_or_reject(self, *, employee_id: uuid.UUID, source: AttendanceSource,
                              recorded_by: uuid.UUID | None, distance_m: float) -> AttendanceLog:
        today = date.today()
        existing = await self.repo.find(employee_id, today)
        if existing:
            # Idempotent + safe: re-tapping check-in same day is a no-op, not a duplicate.
            return existing
        now = self._now()
        cfg = await self._config()
        is_late, _, _ = self._flags(cfg, now, None)
        log = AttendanceLog(
            employee_id=employee_id, work_date=today,
            check_in_at=now, source=source, recorded_by_user_id=recorded_by,
            is_late=is_late, is_short=False, is_overtime=False, distance_m=distance_m,
        )
        return await self.repo.add(log)

    async def _close(self, *, employee_id: uuid.UUID) -> AttendanceLog:
        today = date.today()
        log = await self.repo.find(employee_id, today)
        if not log:
            raise HTTPException(400, "No open check-in to close for today.")
        log.check_out_at = self._now()
        cfg = await self._config()
        log.is_late, log.is_short, log.is_overtime = self._flags(cfg, log.check_in_at, log.check_out_at)
        await self.repo.save(log)
        return log

    # ══════════════════════════════════════════════════════════════════
    # Public helpers for OTHER modules (production + wages)
    # ══════════════════════════════════════════════════════════════════
    async def is_present_today(self, employee_id: uuid.UUID) -> bool:
        """Used by the production service to validate event entry."""
        log = await self.repo.find(employee_id, date.today())
        return log is not None

    async def total_hours(self, employee_id: uuid.UUID, start: date, end: date) -> float:
        """Sum worked hours in the window — the input to daily-wage payroll."""
        rows = await self.repo.for_employee(employee_id, start, end)
        total = 0.0
        for r in rows:
            if r.check_in_at and r.check_out_at:
                total += (r.check_out_at - r.check_in_at).total_seconds() / 3600.0
        return round(total, 2)

    async def days_present(self, employee_id: uuid.UUID, start: date, end: date) -> int:
        """Number of distinct days the worker checked in within the window."""
        return len(await self.repo.for_employee(employee_id, start, end))

    # ══════════════════════════════════════════════════════════════════
    # Reads (dashboards / history)
    # ══════════════════════════════════════════════════════════════════
    async def history(self, employee_id: uuid.UUID, start: date, end: date) -> list[AttendanceLog]:
        return await self.repo.for_employee(employee_id, start, end)

    async def today_roster(self) -> list[AttendanceLog]:
        return await self.repo.by_day(date.today())

    async def get_config(self) -> ShiftConfig:
        return await self._config()

    async def update_config(self, body: schemas.ShiftConfigUpdate) -> ShiftConfig:
        cfg = await self._config()
        for k, v in body.model_dump(exclude_unset=True).items():
            setattr(cfg, k, v)
        await self.repo.save(cfg)
        return cfg
