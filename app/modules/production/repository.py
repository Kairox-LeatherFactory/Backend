"""
================================================================================
modules/production/repository.py — Async data access for production
================================================================================
production_event is the finest grain: one manager records that one employee did
N pieces of one operation on one SKU on one day. Aggregations here power the
live "Carnaby card" (stage totals) and the piece-rate wage inputs.
================================================================================
"""
import uuid
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.clients.models import SKU
from app.modules.production.models import (
    Operation,
    OperationAccess,
    ProductionEvent,
)


class ProductionRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # --- operations / access ---
    async def list_operations(self) -> list[Operation]:
        res = await self.db.execute(
            select(Operation).where(Operation.is_active.is_(True)).order_by(Operation.sequence)
        )
        return list(res.scalars())

    async def get_operation(self, op_id: uuid.UUID) -> Operation | None:
        return await self.db.get(Operation, op_id)

    async def operations_for_role(self, role: str) -> set[uuid.UUID]:
        res = await self.db.execute(
            select(OperationAccess.operation_id).where(OperationAccess.role == role)
        )
        return set(res.scalars())

    # --- events ---
    async def add_event(self, **kw) -> ProductionEvent:
        ev = ProductionEvent(**kw)
        self.db.add(ev)
        await self.db.commit()
        await self.db.refresh(ev)
        return ev

    async def list_events(self, sku_id: uuid.UUID | None = None,
                        employee_id: uuid.UUID | None = None,
                        start: date | None = None,
                        end: date | None = None) -> list[ProductionEvent]:
        stmt = select(ProductionEvent)
        if sku_id:
            stmt = stmt.where(ProductionEvent.sku_id == sku_id)
        if employee_id:
            stmt = stmt.where(ProductionEvent.employee_id == employee_id)
        if start:
            stmt = stmt.where(ProductionEvent.work_date >= start)
        if end:
            stmt = stmt.where(ProductionEvent.work_date <= end)
        res = await self.db.execute(stmt.order_by(ProductionEvent.work_date.desc()))
        return list(res.scalars())

    async def stage_totals_for_style(self, style_id: uuid.UUID) -> dict[str, int]:
        """SUM qty per operation across all SKUs of a style — the live card."""
        stmt = (
            select(Operation.code, func.coalesce(func.sum(ProductionEvent.qty), 0))
            .select_from(ProductionEvent)
            .join(SKU, SKU.id == ProductionEvent.sku_id)
            .join(Operation, Operation.id == ProductionEvent.operation_id)
            .where(SKU.style_id == style_id)
            .group_by(Operation.code)
        )
        res = await self.db.execute(stmt)
        return {code: int(total) for code, total in res.all()}

    async def piece_counts_by_employee_style_op(self, start: date, end: date):
        """Rows of (employee_id, style_id, operation_id, work_date, total_qty) for a
        window — the raw material for piece-rate wage calculation.

        work_date is kept in the grouping ON PURPOSE: a rate can change mid-period,
        so each day's pieces must be priced at the rate effective on THAT day. If we
        collapsed all dates into one total we'd be forced to apply a single rate and
        mis-price work done before/after a rate change."""
        stmt = (
            select(
                ProductionEvent.employee_id,
                SKU.style_id,
                ProductionEvent.operation_id,
                ProductionEvent.work_date,
                func.sum(ProductionEvent.qty),
            )
            .join(SKU, SKU.id == ProductionEvent.sku_id)
            .where(ProductionEvent.work_date >= start, ProductionEvent.work_date <= end)
            .group_by(
                ProductionEvent.employee_id,
                SKU.style_id,
                ProductionEvent.operation_id,
                ProductionEvent.work_date,
            )
        )
        res = await self.db.execute(stmt)
        return res.all()