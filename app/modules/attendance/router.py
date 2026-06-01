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
  GET    /attendance/today               Today's roster (manager/HR view)
  GET    /attendance/config              Read shift + geofence policy
  PATCH  /attendance/config              Manager/HR updates policy
================================================================================
"""
import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.enums import UserRole
from app.modules.attendance import schemas
from app.modules.attendance.service import AttendanceService
from app.modules.users.deps import get_current_user, require_roles
from app.modules.users.models import User

router = APIRouter(prefix="/attendance", tags=["Attendance"])


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
                         user: User = Depends(get_current_user)):
    """Spec Flow B. require_roles isn't used here because the SERVICE enforces
    SUPERVISOR/DIRECT_MANAGER and also validates the worker is daily-wage."""
    return await AttendanceService(db).proxy_mark_present(user, body)


@router.post("/proxy/check-out", response_model=list[schemas.AttendanceRead])
async def proxy_check_out(body: schemas.ProxyMarkRequest,
                          db: AsyncSession = Depends(get_db),
                          user: User = Depends(get_current_user)):
    return await AttendanceService(db).proxy_check_out(user, body)


@router.post("/daily-workers", status_code=201)
async def add_daily_worker(body: schemas.AddDailyWorkerRequest,
                           db: AsyncSession = Depends(get_db),
                           user: User = Depends(get_current_user)):
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


@router.get("/today", response_model=list[schemas.AttendanceRead])
async def today_roster(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await AttendanceService(db).today_roster()


@router.get("/config", response_model=schemas.ShiftConfigRead)
async def get_config(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await AttendanceService(db).get_config()


@router.patch("/config", response_model=schemas.ShiftConfigRead)
async def update_config(
    body: schemas.ShiftConfigUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.DIRECT_MANAGER)),
):
    return await AttendanceService(db).update_config(body)
