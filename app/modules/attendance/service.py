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

import logging
import uuid
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
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
        """SERVER timestamp in UTC (spec: 'blocking client-side time manipulation').

        UTC is the ONLY thing we store. Wall-clock is derived from cfg.timezone
        at the two boundaries that need it: is_late and work_date.
        """
        return datetime.now(timezone.utc)

    @staticmethod
    def _tz(cfg: ShiftConfig) -> ZoneInfo:
        """Factory timezone. Falls back to UTC if the configured name is bad
        (so a typo in config can never crash a check-in)."""
        try:
            return ZoneInfo(cfg.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("UTC")

    @staticmethod
    def _as_utc(dt: datetime) -> datetime:
        """Normalize to a tz-aware UTC instant. SQLite returns naive datetimes
        even for DateTime(timezone=True); we stored UTC, so tag it as UTC."""
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def _local_now(self, cfg: ShiftConfig) -> datetime:
        return self._now().astimezone(self._tz(cfg))

    async def _local_today(self) -> date:
        """The factory's current calendar day. This is the work_date boundary —
        NOT the server's local date — so a punch lands on the right day
        regardless of where the server is hosted."""
        cfg = await self._config()
        return self._local_now(cfg).date()

    def _flags(self, cfg: ShiftConfig, check_in: datetime,
            check_out: datetime | None) -> tuple[bool, bool, bool]:
        """Compute (is_late, is_short, is_overtime) from policy + punches.

        is_late is a WALL-CLOCK comparison, so we convert the (UTC) check-in to
        factory-local time before building shift_start. is_short/is_overtime are
        durations, which are timezone-independent.
        """
        tz = self._tz(cfg)
        local_in = self._as_utc(check_in).astimezone(tz)
        # F109: a malformed shift_start already stored in the DB (from before the
        # schema validation was added) must not crash EVERY check-in. Parse
        # defensively and fall back to a safe default rather than raising.
        try:
            h, m = (int(x) for x in cfg.shift_start.split(":"))
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError(cfg.shift_start)
        except (ValueError, AttributeError):
            logger.warning("invalid shift_start %r in config; defaulting to 09:00",
                           getattr(cfg, "shift_start", None))
            h, m = 9, 0
        shift_start_today = local_in.replace(hour=h, minute=m, second=0, microsecond=0)
        grace = timedelta(minutes=cfg.late_grace_minutes)
        is_late = local_in > (shift_start_today + grace)

        is_short = is_ot = False
        if check_out:
            worked_h = (self._as_utc(check_out) - self._as_utc(check_in)).total_seconds() / 3600.0
            std = float(cfg.shift_length_hours)
            is_short = worked_h < std
            is_ot = worked_h > std
        return is_late, is_short, is_ot

    def _shift_end_at(self, cfg: ShiftConfig, check_in: datetime) -> datetime:
        """When the shift is 'complete' for this punch, as a UTC instant.

        Anchored to check-in + shift_length so it matches the backend's
        is_short/is_overtime calc EXACTLY — this is the target the frontend
        live countdown ticks toward, guaranteeing both agree.
        """
        return self._as_utc(check_in) + timedelta(hours=float(cfg.shift_length_hours))

    # ══════════════════════════════════════════════════════════════════
    # Flow A — Self-service check-in / check-out
    # ══════════════════════════════════════════════════════════════════
    async def self_check_in(self, user: User, body: schemas.CheckInRequest) -> AttendanceLog:
        """The acting user IS the person being marked — an OPERATOR (SECURITY /
        HR / MD / DM) recording their own arrival. Shop-floor workers hold no
        login, so they never take this path; they are scanned in instead."""
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


    async def barcode_scan(self, *, employee_id: uuid.UUID, actor: User,direction: str,
                           lat: float | None,
                           lon: float | None, proxy: bool, reason: str | None = None):
        """Barcode check in/out. Reuses _open_or_reject / _close.

        `actor` is always an operator (SECURITY / HR / MD / DM) — enforced in the
        router before this is called. Workers have no login and cannot reach it.
        """
        emp = await self.employees.get(employee_id)
        if not emp:
            raise HTTPException(404, "Employee not found.")
        # F34 / H6: a MISSING GPS fix must NOT be recorded as distance_m = 0.0,
        # which is indistinguishable from "standing at the gate". When
        # coordinates are present we enforce the fence as normal.
        #
        # H6: when they are absent we no longer just wave the scan through.
        # Indoor/metal-roof floors can genuinely lack a fix, so this is not a
        # hard fail — but "omit two JSON keys and attendance is unverifiable
        # from anywhere" is a bypass, and the resulting row satisfies
        # is_present_today(), which unlocks production logging. So an
        # unverified scan is allowed only as a SUPERVISED exception: it must
        # carry a reason, and PROXY scans (a supervisor standing on the floor)
        # must always carry coordinates.
        if lat is not None and lon is not None:
            dist = await self._enforce_geofence(lat, lon)
        else:
            if proxy:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "A proxy scan must include the supervisor's GPS position.")
            if not (reason or "").strip():
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "Location unavailable — send `reason` to record an "
                    "unverified check-in for supervisor review.")
            dist = None
        source = AttendanceSource.PROXY if proxy else AttendanceSource.SELF
        if direction == "in":
            log = await self._open_or_reject(
                employee_id=employee_id, source=source,
                recorded_by=actor.id, distance_m=dist)
        else:
            log = await self._close(employee_id=employee_id)
        present = await self.is_present_today(employee_id)
        return {
            "employee_id": str(employee_id), "employee_name": emp.name,
            "work_date": log.work_date.isoformat(),
            "check_in_at": log.check_in_at.isoformat() if log.check_in_at else None,
            "check_out_at": log.check_out_at.isoformat() if log.check_out_at else None,
            "is_late": log.is_late, "present_today": present,
            "location_unverified": dist is None,
        }

    # ══════════════════════════════════════════════════════════════════
    # Flow B — Supervisor proxy-marks daily-wage workers
    # ══════════════════════════════════════════════════════════════════
    async def proxy_mark_present(self, operator: User,
                                body: schemas.ProxyMarkRequest) -> list[AttendanceLog]:
        """Manual check-in fallback (card failed / forgotten). Authorised in the
        router (SECURITY / HR / MD / DM). Works for ANY wage type — every employee
        has a card, and any of them can forget it.
 
        The GPS pinged is the OPERATOR's device (they are standing at the gate)."""
        dist = await self._enforce_geofence(body.lat, body.lon)
 
        out: list[AttendanceLog] = []
        for emp_id in body.employee_ids:
            emp = await self.employees.get(emp_id)
            if not emp:
                raise HTTPException(
                    status_code=404, detail=f"Employee {emp_id} not found.")
            # REMOVED: the "not a piece-rate worker — proxy not allowed" refusal.
            # A monthly worker who forgot their card must still get a manual
            # check-in. Manual attendance is a fallback for EVERYONE now.
            log = await self._open_or_reject(
                employee_id=emp.id, source=AttendanceSource.PROXY,
                recorded_by=operator.id, distance_m=dist)
            out.append(log)
        return out

    async def proxy_check_out(self, operator: User,
                              body: schemas.ProxyMarkRequest) -> list[AttendanceLog]:
        """Manual check-out fallback. Authorised in the router. Any wage type."""
        await self._enforce_geofence(body.lat, body.lon)
        out: list[AttendanceLog] = []
        for emp_id in body.employee_ids:
            out.append(await self._close(employee_id=emp_id))
        return out

    # ══════════════════════════════════════════════════════════════════
    # Flow C — Onboard a new daily-wage worker on the floor
    # ══════════════════════════════════════════════════════════════════
    async def add_daily_worker(self, operator: User,
                               body: schemas.AddDailyWorkerRequest) -> Employee:
        """Put a daily-wage worker on the payroll. NO login is created — they get
        an employee record and an employee barcode, and an operator scans them in
        from then on. Stays DM/HR/MD (aligned with the router); SUPERVISOR is
        deliberately not permitted to add people to the payroll."""
        if operator.role not in (
            UserRole.DIRECT_MANAGER, UserRole.HR, UserRole.MANAGING_DIRECTOR):
            raise HTTPException(
                403, "Only a manager or HR may onboard a daily worker.")
        from app.modules.employees.schemas import EmployeeCreate

        emp_create = EmployeeCreate(
            name=body.name,
            designation=body.designation,
            wage_type=WageType.PIECE_RATE,
            phone=body.phone,     # contact detail only; may be None
            email=None,
        )
        return await self.employees.create(emp_create)

    # ══════════════════════════════════════════════════════════════════
    # Open / close primitives (used by both flows)
    # ══════════════════════════════════════════════════════════════════
    async def _open_or_reject(self, *, employee_id: uuid.UUID, source: AttendanceSource,
                            recorded_by: uuid.UUID | None, distance_m: float) -> AttendanceLog:
        cfg = await self._config()
        now = self._now()
        today = now.astimezone(self._tz(cfg)).date()    # factory-local calendar day
        existing = await self.repo.find(employee_id, today)
        if existing:
            # Idempotent + safe: re-tapping check-in same day is a no-op, not a duplicate.
            return existing
        is_late, _, _ = self._flags(cfg, now, None)
        log = AttendanceLog(
            employee_id=employee_id, work_date=today,
            check_in_at=now, source=source, recorded_by_user_id=recorded_by,
            is_late=is_late, is_short=False, is_overtime=False, distance_m=distance_m,
        )
        # F69: uq_att_emp_day makes this a read-then-insert race — two concurrent
        # check-ins (a double-tap, or self racing a proxy) both see no existing
        # row and the second insert would raise an unhandled IntegrityError (500).
        # The constraint is correct; we make the loser the idempotent no-op the
        # comment above already promises: roll back and return the row that won.
        try:
            return await self.repo.add(log)
        except IntegrityError:
            await self.db.rollback()
            existing = await self.repo.find(employee_id, today)
            if existing:
                return existing
            raise

    async def _close(self, *, employee_id: uuid.UUID) -> AttendanceLog:
        today = await self._local_today()
        log = await self.repo.find(employee_id, today)
        if not log:
            raise HTTPException(404, "No check-in recorded today — scan in first.")
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
        log = await self.repo.find(employee_id, await self._local_today())
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

    async def today_roster(self) -> list[schemas.AttendanceRead]:
        return [schemas.AttendanceRead.model_validate(log) for log in await self.repo.by_day(await self._local_today())]

    async def my_status(self, user: User) -> schemas.ShiftStatus:
        """Server-anchored data for the frontend live countdown.

        The frontend must NOT trust the device clock. It computes a one-time
        offset (server_now - device_now) and ticks the remaining time toward
        shift_end_at. All values are UTC; the backend remains the source of
        truth — at check-out it recomputes hours from the stored timestamps.
        """
        cfg = await self._config()
        now = self._now()
        log = None
        if user.employee_id is not None:
            log = await self.repo.find(user.employee_id, now.astimezone(self._tz(cfg)).date())

        check_in_at = self._as_utc(log.check_in_at) if log and log.check_in_at else None
        checked_out = bool(log and log.check_out_at)
        shift_end_at = self._shift_end_at(cfg, check_in_at) if (check_in_at and not checked_out) else None
        remaining = (
            max(0, int((shift_end_at - now).total_seconds())) if shift_end_at else None
        )
        return schemas.ShiftStatus(
            server_now=now,
            timezone=cfg.timezone,
            shift_length_hours=float(cfg.shift_length_hours),
            checked_in=bool(log),
            checked_out=checked_out,
            check_in_at=check_in_at,
            shift_end_at=shift_end_at,
            remaining_seconds=remaining,
        )

    async def get_config(self) -> ShiftConfig:
        return await self._config()

    async def update_config(self, body: schemas.ShiftConfigUpdate) -> ShiftConfig:
        cfg = await self._config()
        data = body.model_dump(exclude_unset=True)
        if "timezone" in data and data["timezone"] is not None:
            try:
                ZoneInfo(data["timezone"])
            except (ZoneInfoNotFoundError, ValueError):
                raise HTTPException(
                    400, f"Unknown timezone '{data['timezone']}'. Use an IANA name like 'Asia/Kolkata'.")
        for k, v in data.items():
            setattr(cfg, k, v)
        await self.repo.save(cfg)
        return cfg