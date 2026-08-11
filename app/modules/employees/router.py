"""
================================================================================
modules/employees/router.py — Employee HTTP API (async)
================================================================================
"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status
from fastapi import HTTPException
import uuid

from app.core.database import get_db
from app.core.enums import UserRole
from app.modules.users.deps import get_current_user, require_roles
from app.modules.employees.service import EmployeeService
from app.modules.employees import schemas
from app.modules.users.models import User

router = APIRouter(prefix="/employees", tags=["Employees"])


@router.get("", response_model=list[schemas.EmployeeReadWithPay|schemas.EmployeeRead])
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
    svc = EmployeeService(db)
    rows = await svc.list_all(active_only)
    # The card code every row is scanned/clicked through by. One extra query for
    # the whole page, not one per employee.
    codes = await svc.barcodes_for([r.id for r in rows])
    pay_roles = {UserRole.HR, UserRole.DIRECT_MANAGER, UserRole.MANAGING_DIRECTOR}
    model = (schemas.EmployeeReadWithPay if user.role in pay_roles
             else schemas.EmployeeRead)
    out = []
    for r in rows:
        row = model.model_validate(r)
        row.employee_barcode = codes.get(r.id)
        out.append(row)
    return out


@router.post("", response_model=schemas.EmployeeCreateRead, status_code=201)
async def create_employee(
    body: schemas.EmployeeCreate,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(
        UserRole.DIRECT_MANAGER, UserRole.HR, UserRole.MANAGING_DIRECTOR)),
):
    """Create an employee. A linked login is minted ONLY when `role` names a
    staff role (managers/HR/security are created HERE now, except DM/MD).

    A plain worker needs name + designation + wage_type and nothing else: no
    phone, no email, no password, no app_user row. They get an employee barcode
    back so their card can be printed and scanned at the gate."""
    return await EmployeeService(db).create(body, actor=actor)

@router.patch("/{employee_id}", response_model=schemas.EmployeeRead)
async def update_employee(
    employee_id: uuid.UUID,
    body: schemas.EmployeeUpdate,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(
        UserRole.DIRECT_MANAGER, UserRole.HR, UserRole.MANAGING_DIRECTOR)),
):
    """Edit an employee (name/designation/wage_type/salary/contact/active).
    Reaches the existing service.update() (was unreachable — F89)."""
    svc = EmployeeService(db)
    emp = await svc.update(employee_id, body, actor=actor)
    out = schemas.EmployeeRead.model_validate(emp)
    # Same shape as the list row — the edit screen keeps showing the card code.
    out.employee_barcode = (await svc.barcodes_for([emp.id])).get(emp.id)
    # role lives on app_user, not on Employee — read it back so a PATCH that
    # granted a login answers with the role it granted instead of null.
    out.role = await svc.login_role_for(emp.id)
    return out
 
 
@router.delete("/{employee_id}", status_code=200)
async def delete_employee(
    employee_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(
        UserRole.DIRECT_MANAGER, UserRole.MANAGING_DIRECTOR)),
):
    """SOFT-delete an employee (is_active=False) + retire their barcode. DM/MD
    only — HR can edit but not remove. History is preserved."""
    return await EmployeeService(db).delete(employee_id, actor_id=user.id)