"""
================================================================================
modules/employees/router.py — Employee HTTP API (async)
================================================================================
"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.enums import UserRole
from app.modules.users.deps import get_current_user, require_roles
from app.modules.employees.service import EmployeeService
from app.modules.employees import schemas
from app.modules.users.models import User

router = APIRouter(prefix="/employees", tags=["Employees"])


@router.get("", response_model=list[schemas.EmployeeRead])
async def list_employees(
    active_only: bool = True,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await EmployeeService(db).list_all(active_only)


@router.post("", response_model=schemas.EmployeeRead, status_code=201)
async def create_employee(
    body: schemas.EmployeeCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.DIRECT_MANAGER)),
):
    return await EmployeeService(db).create(**body.model_dump())
