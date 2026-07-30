"""
================================================================================
modules/employees/repository.py — Async data access for employees
================================================================================
"""
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import WageType
from app.modules.employees.models import Employee


class EmployeeRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_all(self, active_only: bool = True) -> list[Employee]:
        stmt = select(Employee).order_by(Employee.name)
        if active_only:
            stmt = stmt.where(Employee.is_active.is_(True))
        res = await self.db.execute(stmt)
        return list(res.scalars())

    async def get(self, employee_id: uuid.UUID) -> Employee | None:
        return await self.db.get(Employee, employee_id)

    async def list_by_wage_type(self, wt: WageType) -> list[Employee]:
        res = await self.db.execute(
            select(Employee).where(
                Employee.wage_type == wt, Employee.is_active.is_(True)
            )
        )
        return list(res.scalars())

    async def create(self, **kw) -> Employee:
        e = Employee(**kw)
        self.db.add(e)
        await self.db.flush()
        await self.db.refresh(e)
        return e
    
    async def name_exists(self, name: str) -> bool:
        """Case-insensitive existence check. Matches the DB's functional unique
        index on lower(name) so app-level and DB-level agree."""
        found = await self.db.scalar(
            select(Employee.id).where(func.lower(Employee.name) == name.strip().lower()).limit(1)
        )
        return found is not None

    async def save(self, emp: Employee) -> Employee:
        await self.db.commit()
        await self.db.refresh(emp)
        return emp
