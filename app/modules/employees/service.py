"""
================================================================================
modules/employees/service.py — Employee business logic (async)
================================================================================
THREE INVARIANTS THIS FILE OWNS:

  0. A SHOP-FLOOR WORKER GETS NO LOGIN.
     Creating an employee does NOT write app_user, and needs no phone, email or
     password. A login is minted here only when the caller passes an explicit
     STAFF role (schemas._EMPLOYEE_LOGIN_ROLES). Attendance for a worker is
     recorded by an operator (SECURITY / HR / MD / DM) scanning their card, so
     wage_type no longer implies system access — a MONTHLY worker used to be
     auto-given an EMPLOYEE login, and that is exactly what was removed.


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

    async def barcodes_for(self, ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
        """id → ACTIVE card code, for a whole roster in one query.

        The roster screen is also the barcode screen: clicking a row goes to
        PATCH /employees/{employee_id}/barcode (reissue / deactivate), so the
        card code has to be on screen for the user to know WHICH card they are
        retiring. Kept off list_all so the wage run — which reads the same
        roster every payroll — does not pay for a query it never uses.
        """
        if not ids:
            return {}
        from app.modules.barcode.service import BarcodeService
        return await BarcodeService(self.db).employee_codes(ids)

    async def login_role_for(self, employee_id: uuid.UUID) -> UserRole | None:
        """The role of this employee's linked login — None for a plain worker.

        `role` lives on app_user, NOT on Employee (there is no such column, by
        design: an Employee is a payroll identity, a User is a credential). So
        EmployeeRead.role has nothing to read off the ORM row and serialises to
        null unless it is filled in here — including on the response to the very
        PATCH that just minted the login."""
        from app.modules.users.repository import UserRepository
        user = await UserRepository(self.db).get_by_employee(employee_id)
        return user.role if user else None

    # ── name disambiguation ─────────────────────────────────────────────────
    async def _unique_name(self, raw_name: str) -> str:
        """Return a name guaranteed not to collide with an existing employee.

        'RAMESH' free           -> 'RAMESH'
        'RAMESH' taken          -> 'IN-CHAL RAMESH'
        both taken              -> 'IN-CHAL-2 RAMESH'
        ... and so on.

        Comparison is case-insensitive and whitespace-collapsed, because 'ramesh'
        and 'Ramesh ' are the same person to everyone except a database.

        ONE QUERY: colliding_names() returns every taken name this one could
        collide with — the bare name and all its IN-CHAL variants — so the walk
        below is in-memory string work. It used to run a SELECT per candidate,
        up to 100 sequential round trips for a name that collided.

        RACE NOTE: two concurrent creates of the same name can both see 'free'
        here. The DB unique index on lower(name) is the real guard — this walk
        makes the common case produce a MEANINGFUL name instead of a 409. On
        IntegrityError the caller retries; at this factory's create rate
        (a few per week) that path will effectively never fire.
        """
        name = " ".join((raw_name or "").split())
        if not name:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Employee name is required")

        taken = await self.repo.colliding_names(name, IN_CHAL_PREFIX)
        if name.lower() not in taken:
            return name

        candidate = f"{IN_CHAL_PREFIX} {name}"
        if candidate.lower() not in taken:
            return candidate

        for n in range(2, 100):
            candidate = f"{IN_CHAL_PREFIX}-{n} {name}"
            if candidate.lower() not in taken:
                return candidate
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Too many employees named '{name}' — assign a distinct name manually.",
        )

    # ── create ──────────────────────────────────────────────────────────────
    async def create(self, body: schemas.EmployeeCreate,
                     actor=None) -> schemas.EmployeeCreateRead:
        """Create an employee, plus a login ONLY when `role` is a staff role.

        WORKERS GET NO LOGIN — no app_user row, no phone/email needed. wage_type
        is a payroll fact and no longer implies system access: a MONTHLY worker
        used to be auto-given an EMPLOYEE login here, and that is exactly what we
        removed. Every employee still gets an employee barcode, which is how
        SECURITY / HR / MD / DM check them in and out.

        `actor` (the creating User) is used to enforce grant authority: the actor
        may only create a login whose role they are permitted to grant."""
        from app.modules.employees.schemas import _EMPLOYEE_LOGIN_ROLES

        data = body.model_dump(exclude={"password", "role"})
        data["name"] = await self._unique_name(body.name)
        data["designation"] = Designation.normalise(body.designation)
        data["wage_type"] = WageType(body.wage_type)

        emp = await self.repo.create(**data)   # flush only, no commit

        # A login is minted ONLY for an explicit staff role. No fallback.
        login_role = body.role if body.role in _EMPLOYEE_LOGIN_ROLES else None

        user_created = False
        if login_role is not None:
            # grant-authority check: actor may only create roles they can grant.
            if actor is not None:
                allowed = UserService(self.db)._GRANTABLE.get(actor.role, set())
                if login_role not in allowed:
                    raise HTTPException(
                        status.HTTP_403_FORBIDDEN,
                        f"Role '{actor.role.value}' may not create a "
                        f"'{login_role.value}' login.")
            await UserService(self.db).provision_user(
                UserCreate(
                    name=data["name"], phone=body.phone, email=body.email,
                    role=login_role, password=body.password, employee_id=emp.id,
                ),
                must_change_password=True,
            )
            user_created = True
 
        from app.modules.barcode.service import BarcodeService
        code = await BarcodeService(self.db).issue_employee_barcode_nocommit(emp.id, emp.name)
 
        await self.repo.save(emp)  # commit the employee + barcode + user in one txn
        logger.info("Created employee %s (%s) role=%s barcode=%s",
                    emp.name, emp.designation, login_role, code)

        out = schemas.EmployeeCreateRead.model_validate(emp)
        out.user_created = user_created
        out.login_phone = body.phone if user_created else None
        out.employee_barcode = code
        return out

    # ── delete (NEW — soft) ──────────────────────────────────────────────────
    async def delete(self, employee_id: uuid.UUID, actor_id=None) -> dict:
        """SOFT delete. is_active=False (preserves ProductionEvent + wage history)
        and retires the scannable barcode in the same transaction. A hard delete
        is refused by design — it would orphan closed wage lines."""
        emp = await self.repo.get(employee_id)
        if not emp:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Employee not found")
        if not emp.is_active:
            return {"employee_id": str(employee_id), "active": False,
                    "already_inactive": True}
        emp.is_active = False
        # retire the card (nocommit — same txn)
        from app.modules.barcode.service import BarcodeService
        try:
            await BarcodeService(self.db).deactivate_employee_barcode(
                employee_id, actor_id=actor_id)
        except HTTPException:
            # no active card is fine — the employee may never have had one
            pass
        await self.repo.save(emp)
        return {"employee_id": str(employee_id), "active": False,
                "history_preserved": True}

    async def update(self, employee_id: uuid.UUID,
                     body: schemas.EmployeeUpdate, actor=None) -> Employee:
        """Partial update. Designation is re-normalised; name changes re-run the
        uniqueness check."""
        emp = await self.repo.get(employee_id)
        if not emp:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Employee not found")
        data = body.model_dump(exclude_unset=True,
                       exclude={"role", "password"})
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

        if body.role is not None:
            from app.modules.employees.schemas import _EMPLOYEE_LOGIN_ROLES
            from app.modules.users.repository import UserRepository

            actor_role = actor.role if actor is not None else None
            allowed = UserService(self.db)._GRANTABLE.get(actor_role, set())
            if body.role not in _EMPLOYEE_LOGIN_ROLES or body.role not in allowed:
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    f"Role '{actor_role.value if actor_role else 'unknown'}' "
                    "may not create a "
                    f"'{body.role.value}' login.")
            users = UserRepository(self.db)
            if await users.get_by_employee(emp.id):
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "This employee already has an app_user login")
            if not emp.phone:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "phone is required when creating a staff login")
            await UserService(self.db).provision_user(
                UserCreate(name=emp.name, phone=emp.phone, email=emp.email,
                           role=body.role, password=body.password,
                           employee_id=emp.id),
                must_change_password=True)
        await self.repo.save(emp)
        return emp

    # Public interface for the wages / production modules:
    async def get(self, employee_id: uuid.UUID) -> Employee | None:
        return await self.repo.get(employee_id)

    async def monthly_employees(self) -> list[Employee]:
        return await self.repo.list_by_wage_type(WageType.MONTHLY)