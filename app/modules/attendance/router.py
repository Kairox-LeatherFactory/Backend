"""
================================================================================
modules/attendance/router.py — Attendance HTTP API
================================================================================
Endpoints (all mounted under /api/v1):
  POST   /attendance/check-in            Flow A: self check-in (GPS required)
  POST   /attendance/check-out           Flow A: self check-out
  POST   /attendance/proxy/check-in      Flow B: supervisor marks daily workers
  POST   /attendance/proxy/check-out     Flow B: supervisor closes them
  POST   /attendance/daily-workers       Flow C: supervisor onboards a daily worker
  GET    /attendance/me                  Own attendance history (calendar view)
  GET    /attendance/me/status           Live shift status (server-anchored countdown)
  GET    /attendance/today               Today's roster (manager/HR view)
  GET    /attendance/config              Read shift + geofence policy
  PATCH  /attendance/config              Manager/HR updates policy
================================================================================
"""
import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.enums import UserRole
from app.modules.attendance import schemas
from app.modules.attendance.service import AttendanceService
from app.modules.barcode.service import BarcodeService
from app.modules.users.deps import get_current_user, require_roles
from app.modules.users.models import User
from app.modules.attendance.schemas import ScanCheckIn

router = APIRouter(prefix="/attendance", tags=["Attendance"])


@router.post("/scan-check-in")
async def scan_check_in(
    body: ScanCheckIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Check in/out by scanning an employee barcode. A worker may scan only their
    own card; proxy mode is supervisor/manager only (daily-wage workers)."""
    from app.modules.attendance.service import AttendanceService
 
    employee_id = await BarcodeService(db).resolve_employee_id(body.employee_barcode)
 
    proxy_roles = {UserRole.SUPERVISOR, UserRole.DIRECT_MANAGER,
                   UserRole.MANAGING_DIRECTOR, UserRole.HR}
    if user.role == UserRole.EMPLOYEE:
        if getattr(user, "employee_id", None) != employee_id:
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "You can only check in with your own barcode.")
        if body.proxy:
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "Workers cannot proxy for others.")
    elif body.proxy and user.role not in proxy_roles:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Proxy check-in is limited to supervisors and managers.")
 
    return await AttendanceService(db).barcode_scan(
        employee_id=employee_id, actor=user, direction=body.direction,
        lat=body.lat, lon=body.lon, proxy=body.proxy, reason=body.reason)

@router.post("/check-in", response_model=schemas.AttendanceRead, status_code=201)
async def check_in(body: schemas.CheckInRequest,
            db: AsyncSession = Depends(get_db),
            user: User = Depends(get_current_user)):
    return await AttendanceService(db).self_check_in(user, body)


@router.post("/check-out", response_model=schemas.AttendanceRead)
async def check_out(body: schemas.CheckOutRequest,
                    db: AsyncSession = Depends(get_db),
                    user: User = Depends(get_current_user)):
    return await AttendanceService(db).self_check_out(user, body)


@router.post("/proxy/check-in", response_model=list[schemas.AttendanceRead], status_code=201)
async def proxy_check_in(body: schemas.ProxyMarkRequest,
                        db: AsyncSession = Depends(get_db),
                        user: User = Depends(require_roles(UserRole.SUPERVISOR,UserRole.DIRECT_MANAGER,UserRole.HR,UserRole.MANAGING_DIRECTOR))):
    """Spec Flow B. F50: SUPERVISOR is the PRIMARY actor here (daily-wage workers
    carry no phone, so the supervisor marks them present) and must not be locked
    out by the router. The router is now the single authorization decision — the
    service no longer re-checks the role."""
    return await AttendanceService(db).proxy_mark_present(user, body)


@router.post("/proxy/check-out", response_model=list[schemas.AttendanceRead])
async def proxy_check_out(body: schemas.ProxyMarkRequest,
                        db: AsyncSession = Depends(get_db),
                        user: User = Depends(require_roles(UserRole.SUPERVISOR,UserRole.DIRECT_MANAGER,UserRole.HR,UserRole.MANAGING_DIRECTOR))):
    return await AttendanceService(db).proxy_check_out(user, body)


@router.post("/daily-workers", status_code=201)
async def add_daily_worker(body: schemas.AddDailyWorkerRequest,
                        db: AsyncSession = Depends(get_db),
                        user: User = Depends(require_roles(UserRole.DIRECT_MANAGER,UserRole.HR,UserRole.MANAGING_DIRECTOR))):
    """F51: onboarding a daily worker CREATES a login, so this stays at DM/HR/MD
    (NOT widened to SUPERVISOR — granting user-creation to the shop floor is a
    deliberate decision, not a bug fix). The service gate is aligned to match."""
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
    """F44: the geofence geometry (factory_lat/lon/radius) is returned ONLY to
    HR/managers/superusers. Everyone else gets shift times without the fence, so
    a client/employee login cannot read the coordinates needed to forge an
    in-fence check-in."""
    cfg = await AttendanceService(db).get_config()
    privileged = {
        UserRole.DIRECT_MANAGER, UserRole.MANAGING_DIRECTOR, UserRole.HR,
        UserRole.SUPERVISOR, UserRole.CUTTING_MANAGER, UserRole.STITCHING_MANAGER,
        UserRole.LINING_MANAGER,
    }
    if user.role in privileged:
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
    """Attendance history. An EMPLOYEE can only ever read their own — the
    employee_id parameter is ignored for them rather than 403'd, so the same
    frontend call works for every role. F36: only HR/managers/superusers may
    read ANOTHER employee's history; CLIENT and VIEWER cannot reach it."""
    if user.role is UserRole.EMPLOYEE:
        if user.employee_id is None:
            raise HTTPException(400, "This login is not linked to an employee record.")
        employee_id = user.employee_id
    elif user.role not in (
        UserRole.DIRECT_MANAGER, UserRole.MANAGING_DIRECTOR, UserRole.HR,
        UserRole.SUPERVISOR, UserRole.CUTTING_MANAGER, UserRole.STITCHING_MANAGER,
        UserRole.LINING_MANAGER,
    ):
        # CLIENT, VIEWER, or any future role: no access to workers' attendance.
        raise HTTPException(403, "Not permitted to read attendance history.")
    elif employee_id is None:
        raise HTTPException(422, "employee_id is required")
    return await AttendanceService(db).history(employee_id, start, end)


@router.get("/today", response_model=list[schemas.AttendanceRead])
async def today_roster(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(
        UserRole.SUPERVISOR, UserRole.HR, UserRole.CUTTING_MANAGER,
        UserRole.STITCHING_MANAGER, UserRole.LINING_MANAGER)),
):
    """Whole-floor roster. Never visible to an EMPLOYEE."""
    return await AttendanceService(db).today_roster()