"""
================================================================================
modules/wages/service.py — Wage-calc engine (async)
================================================================================
Forks on wage_type:
  piece_rate : sum(qty * effective_rate(style, op, work_date)) from production —
               each day's pieces are priced at the rate effective on THAT day, so
               a mid-period rate change splits correctly across earlier/later work.
  monthly    : a single line of monthly_salary, independent of production
Results are FROZEN into wage_line rows so a closed run never recomputes.
================================================================================
"""
import uuid
from collections import defaultdict
from datetime import date

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import WageType
from app.modules.employees.service import EmployeeService
from app.modules.production.service import ProductionService
from app.modules.wages.models import WageLine, WageRun
from app.modules.wages.repository import WageRepository


class WageService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = WageRepository(db)
        self.production = ProductionService(db)
        self.employees = EmployeeService(db)

    async def set_rate(self, style_id, operation_id, rate, effective_from):
        return await self.repo.upsert_rate(style_id, operation_id, rate, effective_from)

    async def compute_run(self, period_start: date, period_end: date) -> WageRun | None:
        """Compute and FREEZE payroll for a window. Returns a closed run."""
        run = await self.repo.create_run(period_start, period_end)
        
        lines: list[WageLine] = []
        per_emp_amount: dict[uuid.UUID, float] = defaultdict(float)
        per_emp_pieces: dict[uuid.UUID, int] = defaultdict(int)

        # piece-rate population. Rows are grouped per (emp, style, op, work_date) so
        # each day is priced at the rate effective on THAT day — a rate change inside
        # the period prices earlier work at the old rate and later work at the new one.
        # rate_cache collapses the per-row effective_rate lookups: most employees
        # share the same (style, op, work_date), so we hit the DB only once per key.
        rate_cache: dict[tuple[uuid.UUID, uuid.UUID, date], float | None] = {}
        rows = await self.production.piece_counts(period_start, period_end)
        for emp_id, style_id, op_id, work_date, qty in rows:
            key = (style_id, op_id, work_date)
            if key not in rate_cache:
                rate_cache[key] = await self.repo.effective_rate(style_id, op_id, work_date)
            rate = rate_cache[key]
            if rate is None:
                continue          # no rate configured — skip
            per_emp_amount[emp_id] += float(qty) * rate
            per_emp_pieces[emp_id] += int(qty)

        for emp_id, amount in per_emp_amount.items():
            lines.append(WageLine(
                wage_run_id=run.id, employee_id=emp_id, wage_type=WageType.PIECE_RATE,
                pieces=per_emp_pieces[emp_id], amount=round(amount, 2),
            ))

        # monthly population (independent of production)
        for emp in await self.employees.monthly_employees():
            lines.append(WageLine(
                wage_run_id=run.id, employee_id=emp.id, wage_type=WageType.MONTHLY,
                pieces=0, amount=float(emp.monthly_salary or 0),
            ))

        await self.repo.add_lines(lines)
        return await self.repo.close_run(run)

    async def get_run(self, run_id: uuid.UUID) -> WageRun | None:
        run = await self.repo.get_run(run_id)
        if not run:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Wage run not found")
        return run