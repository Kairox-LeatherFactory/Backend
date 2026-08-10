"""
================================================================================
modules/employees/schemas.py — API contract for the employees module
================================================================================
WORKERS GET NO LOGIN. A shop-floor employee is a payroll/production identity
only — there is no app_user row, so phone and email are genuinely optional and
a password is never accepted. Their attendance is entered by an operator
(SECURITY / HR / MD / DM) scanning their employee card.

The ONLY people created here who also get a login are STAFF: pass an explicit
`role` from _EMPLOYEE_LOGIN_ROLES and the login is minted in the same
transaction (that path does need phone + password).
================================================================================
"""
import uuid

from pydantic import BaseModel, ConfigDict, model_validator

from app.core.enums import UserRole, WageType

# Staff roles that may be created through the employee door (they are real
# people on the payroll AND hold a login). DM/MD are excluded — they belong to
# user-creation. EMPLOYEE is excluded because workers get no login at all.
_EMPLOYEE_LOGIN_ROLES = {
    UserRole.HR, UserRole.SUPERVISOR, UserRole.CUTTING_MANAGER,
    UserRole.LINING_MANAGER, UserRole.STITCHING_MANAGER, UserRole.SECURITY,
    UserRole.MERCHANDISER,
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
    role: UserRole | None = None
    # The worker's ACTIVE card code (EMP-000123). None when the card was
    # retired (leaver) or never issued — a retired code is not scannable, so
    # showing it would invite a scan that resolves 410 Gone.
    employee_barcode: str | None = None


class EmployeeReadWithPay(EmployeeRead):
    """Roster view WITH pay. Returned only to HR / DM / MD (F37)."""
    monthly_salary: float | None = None


class EmployeeCreate(BaseModel):
    """Create a person on the payroll.

    Plain worker (the common case): name + designation + wage_type is enough.
    NO phone, NO email, NO password, NO role — they get no system access, only
    an employee barcode so their card can be scanned.

    Staff: pass `role` (one of _EMPLOYEE_LOGIN_ROLES) and a login is minted
    alongside the employee record; that path needs phone + password.
    """
    name: str
    designation: str | None = None
    wage_type: WageType = WageType.PIECE_RATE
    monthly_salary: float | None = None
    phone: str | None = None        # optional contact; required only for staff
    email: str | None = None        # optional contact
    password: str | None = None     # staff logins only
    role: UserRole | None = None    # staff login role; omit for a worker

    @model_validator(mode="after")
    def _login_fields(self):
        # WORKERS GET NO LOGIN. wage_type no longer decides this — a MONTHLY
        # worker is still just a worker, paid differently. Only an explicit
        # STAFF role mints a login, and only that path needs credentials.
        if self.role is UserRole.EMPLOYEE:
            raise ValueError(
                "The 'employee' role is not assignable — shop-floor workers "
                "have no login. Omit `role` to create a worker.")
        # DM/MD may not be minted here — they belong to user-creation.
        if self.role in (UserRole.MANAGING_DIRECTOR, UserRole.DIRECT_MANAGER):
            raise ValueError(
                "DIRECT_MANAGER and MANAGING_DIRECTOR logins are created via "
                "user-creation, not the employee service.")
        if self.role is not None and self.role not in _EMPLOYEE_LOGIN_ROLES:
            raise ValueError(
                f"Role '{self.role.value}' cannot be created here. Permitted: "
                f"{', '.join(sorted(r.value for r in _EMPLOYEE_LOGIN_ROLES))}.")

        if self.role in _EMPLOYEE_LOGIN_ROLES:
            missing = [f for f in ("phone", "password") if not getattr(self, f)]
            if missing:
                raise ValueError(
                    f"{', '.join(missing)} required when creating a staff login")
        elif self.password:
            raise ValueError(
                "password is only accepted when a staff login is created — "
                "workers do not log in")
        return self


class EmployeeCreateRead(EmployeeRead):
    # employee_barcode is inherited from EmployeeRead — create sets it to the
    # code minted in the same transaction so the card can be printed.
    user_created: bool = False
    login_phone: str | None = None



class EmployeeUpdate(BaseModel):
    name: str | None = None
    designation: str | None = None
    wage_type: WageType | None = None
    monthly_salary: float | None = None
    phone: str | None = None
    email: str | None = None
    is_active: bool | None = None