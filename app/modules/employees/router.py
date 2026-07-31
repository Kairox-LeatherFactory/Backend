"""
================================================================================
modules/employees/router.py — Employee HTTP API (async)
================================================================================
"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status
from fastapi import HTTPException

from app.core.database import get_db
from app.core.enums import UserRole
from app.modules.users.deps import get_current_user, require_roles
from app.modules.employees.service import EmployeeService
from app.modules.employees import schemas
from app.modules.users.models import User

router = APIRouter(prefix="/employees", tags=["Employees"])


@router.get("")
async def list_employees(
    active_only: bool = True,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """F37: salary is returned ONLY to HR / DM / MD.

    H3: the roster itself is internal. EmployeeRead still carries phone and
    email (schemas.py:27-28), so redacting salary alone was not enough — a
    CLIENT token was receiving staff PII."""
    if user.role in {UserRole.CLIENT, UserRole.VIEWER}:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "The employee roster is internal.")
    rows = await EmployeeService(db).list_all(active_only)
    pay_roles = {UserRole.HR, UserRole.DIRECT_MANAGER, UserRole.MANAGING_DIRECTOR}
    if user.role in pay_roles:
        return [schemas.EmployeeReadWithPay.model_validate(r) for r in rows]
    return [schemas.EmployeeRead.model_validate(r) for r in rows]


@router.post("", response_model=schemas.EmployeeCreateRead, status_code=201)
async def create_employee(
    body: schemas.EmployeeCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.DIRECT_MANAGER,UserRole.HR,UserRole.MANAGING_DIRECTOR)),
):
    return await EmployeeService(db).create(body)