"""
================================================================================
modules/employees/service.py — Employee business logic (async)
================================================================================
Exposes the public interface the wages module depends on (monthly_employees, get).
================================================================================
"""
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import WageType
from app.modules.employees.models import Employee
from app.modules.employees.repository import EmployeeRepository


class EmployeeService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = EmployeeRepository(db)

    async def list_all(self, active_only: bool = True) -> list[Employee]:
        return await self.repo.list_all(active_only)

    async def create(self, **kw) -> Employee:
        return await self.repo.create(**kw)

    # Public interface for the wages module:
    async def get(self, employee_id: uuid.UUID) -> Employee | None:
        return await self.repo.get(employee_id)

    async def monthly_employees(self) -> list[Employee]:
        return await self.repo.list_by_wage_type(WageType.MONTHLY)
