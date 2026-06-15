"""
================================================================================
modules/wages/repository.py — Async data access for rates & wage runs
================================================================================
effective_rate picks the latest rate <= work_date (effective-dated rates).
A CLOSED run is a frozen snapshot of wage_line rows — never recomputed.
================================================================================
"""
import uuid
from datetime import date

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.enums import RunStatus
from app.modules.wages.models import Rate, WageLine, WageRun


class WageRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def effective_rate(self, style_id: uuid.UUID, operation_id: uuid.UUID,
                            on: date) -> float | None:
        stmt = (
            select(Rate.rate)
            .where(
                Rate.style_id == style_id,
                Rate.operation_id == operation_id,
                Rate.effective_from <= on,
            )
            .order_by(Rate.effective_from.desc())
            .limit(1)
        )
        val = await self.db.scalar(stmt)
        return float(val) if val is not None else None

    async def upsert_rate(self, style_id, operation_id, rate, effective_from) -> Rate:
        existing = await self.db.scalar(select(Rate).where(and_(
            Rate.style_id == style_id, Rate.operation_id == operation_id,
            Rate.effective_from == effective_from)))
        if existing:
            existing.rate = rate
            await self.db.commit()
            await self.db.refresh(existing)
            return existing
        r = Rate(style_id=style_id, operation_id=operation_id,
                rate=rate, effective_from=effective_from)
        self.db.add(r)
        await self.db.commit()
        await self.db.refresh(r)
        return r
    
    async def create_run(self, period_start: date, period_end: date) -> WageRun:
        run = WageRun(period_start=period_start, period_end=period_end)
        self.db.add(run)
        await self.db.commit()
        await self.db.refresh(run)
        return run

    async def get_run(self, run_id: uuid.UUID) -> WageRun | None:
        stmt = select(WageRun).where(WageRun.id == run_id).options(selectinload(WageRun.lines))
        return await self.db.scalar(stmt)

    async def add_lines(self, lines: list[WageLine]) -> None:
        self.db.add_all(lines)
        await self.db.commit()

    async def close_run(self, run: WageRun) -> WageRun | None:
        run.status = RunStatus.CLOSED
        await self.db.commit()
        # Re-load with lines eagerly so the response serialises cleanly.
        return await self.get_run(run.id)