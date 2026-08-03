"""
================================================================================
modules/employees/schemas.py — API contract for the employees module
================================================================================
phone/email are MOCKED at seed time (the source files carry no contact data);
they are surfaced here so a manager can provision logins and contact workers.
================================================================================
"""
import uuid

from pydantic import BaseModel, ConfigDict, model_validator

from app.core.enums import UserRole, WageType

_EMPLOYEE_LOGIN_ROLES = {
    UserRole.HR, UserRole.SUPERVISOR, UserRole.CUTTING_MANAGER,
    UserRole.LINING_MANAGER, UserRole.STITCHING_MANAGER, UserRole.SECURITY,
}


class EmployeeRead(BaseModel):
    """Roster view WITHOUT pay. Safe for any internal reader. F37: monthly_salary
    is NOT here — it was previously exposed to every non-employee role including
    CLIENT and VIEWER."""
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    designation: str | None
    wage_type: WageType
    is_active: bool
    phone: str | None = None
    email: str | None = None


class EmployeeReadWithPay(EmployeeRead):
    """Roster view WITH pay. Returned only to HR / DM / MD (F37)."""
    monthly_salary: float | None = None


class EmployeeCreate(BaseModel):
    name: str
    designation: str | None = None
    wage_type: WageType = WageType.PIECE_RATE
    monthly_salary: float | None = None
    phone: str | None = None
    email: str | None = None
    password: str | None = None
    role: UserRole | None = None          # NEW: manager/HR login role, optional
 
    @model_validator(mode="after")
    def _login_fields(self):
        # A staff LOGIN role (manager/HR/etc.) or a MONTHLY wage requires
        # credentials, because both mint a login.
        needs_login = (self.wage_type is WageType.MONTHLY
                       or self.role in _EMPLOYEE_LOGIN_ROLES)
        if needs_login:
            missing = [f for f in ("phone", "password") if not getattr(self, f)]
            if missing:
                raise ValueError(
                    f"{', '.join(missing)} required when creating a login "
                    f"(MONTHLY wage or a staff role)")
        elif self.password:
            raise ValueError("password is only accepted when a login is created")
        # DM/MD may not be minted here — they belong to user-creation.
        if self.role in (UserRole.MANAGING_DIRECTOR, UserRole.DIRECT_MANAGER):
            raise ValueError(
                "DIRECT_MANAGER and MANAGING_DIRECTOR logins are created via "
                "user-creation, not the employee service.")
        return self


class EmployeeCreateRead(EmployeeRead):
    user_created: bool = False
    login_phone: str | None = None
    employee_barcode: str | None = None
    
class EmployeeUpdate(BaseModel):
    name: str | None = None
    designation: str | None = None
    wage_type: WageType | None = None
    monthly_salary: float | None = None
    phone: str | None = None
    email: str | None = None
    is_active: bool | None = None