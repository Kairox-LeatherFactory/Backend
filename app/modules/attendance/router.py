"""
================================================================================
modules/attendance/router.py — Attendance HTTP API
================================================================================
WHO MAY WRITE ATTENDANCE
  ONLY an operator login: SECURITY / HR / MANAGING_DIRECTOR / DIRECT_MANAGER
  (core.enums.ATTENDANCE_OPERATOR_ROLES). Shop-floor workers have NO login at
  all any more, so there is no such thing as a worker checking themselves in:
  an operator scans the worker's card (or types them in when the card fails).

LOCATION TRACKING IS REMOVED
  No route on this router asks for, validates or stores a position. Check-in and
  check-out succeed on identity alone. `lat`/`lon` are still accepted on the
  bodies that used to require them, and ignored — see attendance/schemas.py.

Endpoints (all mounted under /api/v1):
  POST   /attendance/scan-check-in       Barcode door: operator scans a card, in|out
  POST   /attendance/check-in            Operator's OWN check-in (no body needed)
  POST   /attendance/check-out           Operator's OWN check-out
  POST   /attendance/proxy/check-in      Manual door: operator types employees in
  POST   /attendance/proxy/check-out     Manual door: operator closes them
  POST   /attendance/daily-workers       Onboard a daily worker (DM/HR/MD)
  GET    /attendance/me                  Own attendance history (calendar view)
  GET    /attendance/me/status           Live shift status (server-anchored countdown)
  GET    /attendance/today               Today's roster (manager/HR view)
  GET    /attendance/config              Read shift policy
  PATCH  /attendance/config              Manager/HR updates policy
================================================================================
"""
import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.enums import ATTENDANCE_OPERATOR_ROLES, UserRole
from app.modules.attendance import schemas
from app.modules.attendance.service import AttendanceService
from app.modules.barcode.service import BarcodeService
from app.modules.users.deps import get_current_user, require_roles
from app.modules.users.models import User
from app.modules.attendance.schemas import ScanCheckIn

router = APIRouter(prefix="/attendance", tags=["Attendance"])

# One dependency, used by every attendance WRITE route, so the rule cannot drift
# between the barcode door and the manual door.
require_operator = require_roles(*ATTENDANCE_OPERATOR_ROLES)

# Roles that may read OTHER people's attendance (the floor leads need the roster
# even though they may not write punches).
_ATTENDANCE_READERS = {
    *ATTENDANCE_OPERATOR_ROLES, UserRole.SUPERVISOR,
    UserRole.CUTTING_MANAGER, UserRole.STITCHING_MANAGER, UserRole.LINING_MANAGER,
}


@router.post("/scan-check-in")
async def scan_check_in(
    body: ScanCheckIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator),
):
    """Check in/out by scanning an employee barcode (direction = "in" | "out").

    No position is requested or stored — `lat`/`lon` are accepted and ignored.

    THE primary attendance door. Every employee has a card and no employee has a
    login, so the operator standing at the gate — SECURITY / HR / MD / DM — scans
    every card, monthly or daily, including their own colleagues'.
    `proxy=True` marks the MANUAL fallback (card won't scan / forgotten)."""
    employee_id = await BarcodeService(db).resolve_employee_id(body.employee_barcode)
    return await AttendanceService(db).barcode_scan(
        employee_id=employee_id, actor=user, direction=body.direction,
        lat=body.lat, lon=body.lon, proxy=body.proxy, reason=body.reason)


@router.post("/check-in", response_model=schemas.AttendanceRead, status_code=201)
async def check_in(body: schemas.CheckInRequest | None = None,
            db: AsyncSession = Depends(get_db),
            user: User = Depends(require_operator)):
    """An OPERATOR records their own arrival (their login is linked to an
    employee row). Workers never reach this — they have no login.

    The body is OPTIONAL: with the geofence removed there is nothing to send.
    `{}`, `{"lat": .., "lon": ..}` (ignored) and no body at all all work."""
    return await AttendanceService(db).self_check_in(user, body)


@router.post("/check-out", response_model=schemas.AttendanceRead)
async def check_out(body: schemas.CheckOutRequest | None = None,
                    db: AsyncSession = Depends(get_db),
                    user: User = Depends(require_operator)):
    """An OPERATOR records their own departure. Body optional — see check_in."""
    return await AttendanceService(db).self_check_out(user, body)


# Manual check-in fallback (no card scan — operator types the employee in).
# Reuses ProxyMarkRequest (employee_ids + gps). Same four operators as the
# barcode door, any wage type. Direction handled by the two routes below.

@router.post("/proxy/check-in", response_model=list[schemas.AttendanceRead], status_code=201)
async def proxy_check_in(
    body: schemas.ProxyMarkRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator),
):
    """Manual check-in fallback for one or more employees (card failed / forgotten).
    SECURITY / HR / MD / DM. Works for ANY wage type. Only `employee_ids` is
    required — the operator's position is no longer asked for."""
    return await AttendanceService(db).proxy_mark_present(user, body)


@router.post("/proxy/check-out", response_model=list[schemas.AttendanceRead])
async def proxy_check_out(
    body: schemas.ProxyMarkRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator),
):
    """Manual check-out fallback. Same four operators, any wage type."""
    return await AttendanceService(db).proxy_check_out(user, body)


@router.post("/daily-workers", status_code=201)
async def add_daily_worker(body: schemas.AddDailyWorkerRequest,
                        db: AsyncSession = Depends(get_db),
                        user: User = Depends(require_roles(UserRole.DIRECT_MANAGER,UserRole.HR,UserRole.MANAGING_DIRECTOR))):
    """Onboard a daily-wage worker on the floor. No login is created any more —
    the worker gets an employee record and a printable card, nothing else. Stays
    at DM/HR/MD (NOT widened to SUPERVISOR): adding people to the payroll is a
    deliberate authority. The service gate is aligned to match."""
    emp = await AttendanceService(db).add_daily_worker(user, body)
    return {"id": str(emp.id), "name": emp.name, "wage_type": emp.wage_type.value}


@router.get("/me", response_model=list[schemas.AttendanceRead])
async def my_history(
    start: date | None = Query(None), end: date | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.employee_id is None:
        return []
    end = end or date.today()
    start = start or (end - timedelta(days=30))
    return await AttendanceService(db).history(user.employee_id, start, end)


@router.get("/me/status", response_model=schemas.ShiftStatus)
async def my_status(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Server-anchored data for the live shift countdown. Frontend computes a
    one-time (server_now - device_now) offset and ticks toward shift_end_at."""
    return await AttendanceService(db).my_status(user)


@router.get("/config")
async def get_config(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Shift policy — start time, length, grace, timezone.

    The privileged/public split (F44) existed to keep the fence coordinates away
    from non-managers. LOCATION IS REMOVED, so both shapes now return the same
    fields; the branch is kept so the split returns intact if the fence does."""
    cfg = await AttendanceService(db).get_config()
    if user.role in _ATTENDANCE_READERS:
        return schemas.ShiftConfigRead.model_validate(cfg)
    return schemas.ShiftConfigPublicRead.model_validate(cfg)


@router.patch("/config", response_model=schemas.ShiftConfigRead)
async def update_config(
    body: schemas.ShiftConfigUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.DIRECT_MANAGER,UserRole.HR,UserRole.MANAGING_DIRECTOR)),
):
    return await AttendanceService(db).update_config(body)

@router.get("/history", response_model=list[schemas.AttendanceRead])
async def history(
    start: date, end: date,
    employee_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Attendance history for one employee.

    F36: only operators / floor leads may read a worker's history; CLIENT and
    VIEWER cannot reach it. (The self-service branch is gone with the employee
    login — a worker's own history is read by whoever is helping them, and
    /attendance/me still serves a staff login's own record.)"""
    if user.role not in _ATTENDANCE_READERS:
        raise HTTPException(403, "Not permitted to read attendance history.")
    if employee_id is None:
        raise HTTPException(422, "employee_id is required")
    return await AttendanceService(db).history(employee_id, start, end)


@router.get("/today", response_model=list[schemas.AttendanceRead])
async def today_roster(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(*_ATTENDANCE_READERS)),
):
    """Whole-floor roster. Operators + floor leads only."""
    return await AttendanceService(db).today_roster()