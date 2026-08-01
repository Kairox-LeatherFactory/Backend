"""
================================================================================
modules/employees/service.py — Employee business logic (async)
================================================================================
TWO INVARIANTS THIS FILE OWNS:

  1. DESIGNATION IS ALWAYS UPPERCASE, normalised.
     'shell tailor' / 'Shell-Tailor' / ' SHELL TAILOR ' all become SHELL_TAILOR.
     This is not cosmetic: production's skill gate does a set membership test on
     the designation, and three spellings of one job title means three skills and
     no gate.

  2. NAME IS UNIQUE, disambiguated with an IN-CHAL PREFIX.
     Two 'RAMESH' rows are a wage-misattribution waiting to happen — a manager
     picks the wrong one from a dropdown and the piece money lands in the wrong
     envelope. On collision the NEW employee is stored as 'IN-CHAL RAMESH',
     then 'IN-CHAL-2 RAMESH', and so on. The EXISTING row is never renamed:
     it is already printed on wage slips and referenced in closed runs.
================================================================================
"""
import logging
import uuid

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import Designation, UserRole, WageType
from app.modules.employees import schemas
from app.modules.employees.models import Employee
from app.modules.employees.repository import EmployeeRepository
from app.modules.users.schemas import UserCreate
from app.modules.users.service import UserService

logger = logging.getLogger(__name__)

IN_CHAL_PREFIX = "IN-CHAL"


class EmployeeService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = EmployeeRepository(db)

    # employees/service.py — add near list_all
    async def names_for(self, ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
        """id → name for a set of employees (payroll warning display)."""
        if not ids:
            return {}
        rows = await self.repo.list_all(active_only=False)
        return {e.id: e.name for e in rows if e.id in set(ids)}

    async def list_all(self, active_only: bool = True) -> list[Employee]:
        return await self.repo.list_all(active_only)

    # ── name disambiguation ─────────────────────────────────────────────────
    async def _unique_name(self, raw_name: str) -> str:
        """Return a name guaranteed not to collide with an existing employee.

        'RAMESH' free           -> 'RAMESH'
        'RAMESH' taken          -> 'IN-CHAL RAMESH'
        both taken              -> 'IN-CHAL-2 RAMESH'
        ... and so on.

        Comparison is case-insensitive and whitespace-collapsed, because 'ramesh'
        and 'Ramesh ' are the same person to everyone except a database.

        RACE NOTE: two concurrent creates of the same name can both see 'free'
        here. The DB unique index on lower(name) is the real guard — this loop
        makes the common case produce a MEANINGFUL name instead of a 409. On
        IntegrityError the caller retries; at this factory's create rate
        (a few per week) that path will effectively never fire.
        """
        name = " ".join((raw_name or "").split())
        if not name:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Employee name is required")
        if not await self.repo.name_exists(name):
            return name

        candidate = f"{IN_CHAL_PREFIX} {name}"
        if not await self.repo.name_exists(candidate):
            return candidate

        n = 2
        while n < 100:
            candidate = f"{IN_CHAL_PREFIX}-{n} {name}"
            if not await self.repo.name_exists(candidate):
                return candidate
            n += 1
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Too many employees named '{name}' — assign a distinct name manually.",
        )

    # ── create ──────────────────────────────────────────────────────────────
    async def create(self, body: schemas.EmployeeCreate) -> schemas.EmployeeCreateRead:
        data = body.model_dump(exclude={"password"})
        data["name"] = await self._unique_name(body.name)
        data["designation"] = Designation.normalise(body.designation)
        data["wage_type"] = WageType(body.wage_type)
 
        emp = await self.repo.create(**data)   # flush only, no commit
 
        user_created = False
        if data["wage_type"] is WageType.MONTHLY:
            await UserService(self.db).provision_user(
                UserCreate(
                    name=data["name"], phone=body.phone, email=body.email,
                    role=UserRole.EMPLOYEE, password=body.password,
                    employee_id=emp.id,
                ),
                must_change_password=True,
            )
            user_created = True
 
        # issue the scannable card (nocommit — same transaction as the employee)
        from app.modules.barcode.service import BarcodeService
        code = await BarcodeService(self.db).issue_employee_barcode_nocommit(
            emp.id, emp.name)
 
        await self.db.commit()
        await self.db.refresh(emp)
        logger.info("Created employee %s (%s) barcode=%s", emp.name, emp.designation, code)
 
        # return the read model WITH the barcode so the UI can print the card
        out = schemas.EmployeeCreateRead.model_validate(emp)
        out.user_created = user_created
        out.login_phone = body.phone if user_created else None
        out.employee_barcode = code
        return out

    async def update(self, employee_id: uuid.UUID,
                     body: schemas.EmployeeUpdate) -> Employee:
        """Partial update. Designation is re-normalised; name changes re-run the
        uniqueness check."""
        emp = await self.repo.get(employee_id)
        if not emp:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Employee not found")
        data = body.model_dump(exclude_unset=True)
        if "designation" in data:
            data["designation"] = Designation.normalise(data["designation"])
        if "name" in data and " ".join(data["name"].split()).lower() != emp.name.lower():
            data["name"] = await self._unique_name(data["name"])
        if "wage_type" in data:
            data["wage_type"] = WageType(data["wage_type"])
        # F47: enumerate the writable fields explicitly. Even though no PATCH route
        # currently reaches this method (F89), the mass-setattr would become a live
        # privilege/payroll write the moment one is added (EmployeeUpdate carries
        # is_active and monthly_salary). Only these fields may be set here.
        _ALLOWED = {"name", "designation", "wage_type", "monthly_salary",
                    "phone", "email", "is_active"}
        rejected = set(data) - _ALLOWED
        if rejected:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Fields not updatable here: {', '.join(sorted(rejected))}.")
        for k, v in data.items():
            if k in _ALLOWED:
                setattr(emp, k, v)
        await self.repo.save(emp)
        return emp

    # Public interface for the wages / production modules:
    async def get(self, employee_id: uuid.UUID) -> Employee | None:
        return await self.repo.get(employee_id)

    async def monthly_employees(self) -> list[Employee]:
        return await self.repo.list_by_wage_type(WageType.MONTHLY)