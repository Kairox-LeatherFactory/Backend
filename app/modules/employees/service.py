"""
================================================================================
modules/employees/service.py — Employee business logic (async)
================================================================================
Exposes the public interface the wages module depends on (monthly_employees, get).
================================================================================
"""
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import UserRole, WageType
from app.modules.employees.models import Employee
from app.modules.employees.repository import EmployeeRepository
from app.modules.employees import schemas
from app.modules.users.models import User
from app.modules.users.service import UserService
from app.modules.users.schemas import UserCreate


class EmployeeService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = EmployeeRepository(db)

    async def list_all(self, active_only: bool = True) -> list[Employee]:
        return await self.repo.list_all(active_only)

    async def create(self, body: schemas.EmployeeCreate) -> tuple[Employee, User | None]:
        emp = await self.repo.create(**body.model_dump(exclude={"password"}))  # flush only
        user = None
        if body.wage_type is WageType.MONTHLY:
            user = await UserService(self.db).provision_user(
               UserCreate(
                    name=body.name, phone=body.phone, email=body.email,
                    role=UserRole.EMPLOYEE, password=body.password, employee_id=emp.id,
                ),
                must_change_password=True,   # manager typed it → worker must rotate it
            )
        await self.db.commit()
        await self.db.refresh(emp)
        return emp, user

    # Public interface for the wages module:
    async def get(self, employee_id: uuid.UUID) -> Employee | None:
        return await self.repo.get(employee_id)

    async def monthly_employees(self) -> list[Employee]:
        return await self.repo.list_by_wage_type(WageType.MONTHLY)
