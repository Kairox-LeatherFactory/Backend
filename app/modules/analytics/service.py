"""
================================================================================
modules/analytics/service.py — Read-only cross-module analytics (async)
================================================================================
Powers the live dashboard, the sea-freight risk predictor, stage-spread
bottleneck detection, and the per-piece production feed / traveler view.

This module is allowed to read across modules' tables (read-only) for aggregate
reporting. It never writes. All display rows are built with explicit joins — no
lazy relationship access under async.
================================================================================
"""
import uuid
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.enums import ShipMode
from app.modules.clients.models import SKU, Client, ClientOrder, Style
from app.modules.clients.service import sku_label
from app.modules.employees.models import Employee
from app.modules.production.models import Operation, Piece, ProductionEvent


class AnalyticsService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def factory_overview(self) -> dict:
        total_ordered = await self.db.scalar(
            select(func.coalesce(func.sum(SKU.qty_ordered), 0))
        ) or 0
        total_produced = await self.db.scalar(
            select(func.coalesce(func.sum(ProductionEvent.qty), 0))
        ) or 0
        clients = await self.db.scalar(select(func.count(Client.id))) or 0
        styles = await self.db.scalar(select(func.count(Style.id))) or 0
        return {
            "clients": int(clients),
            "styles": int(styles),
            "total_pieces_ordered": int(total_ordered),
            "total_operations_logged": int(total_produced),
        }

    # ------------------------------------------------------- per-piece feed
    async def production_feed(
        self, *, piece_code: str | None = None, employee_id: uuid.UUID | None = None,
        operation_id: uuid.UUID | None = None, style_id: uuid.UUID | None = None,
        order_id: uuid.UUID | None = None, start: date | None = None,
        end: date | None = None, limit: int = 500,
    ) -> list[dict]:
        """One row per piece-scan: who did what stage to which piece, when.

        This is the 'track each piece through every stage' view. Filter by
        piece_code to get a single piece's full history (see piece_history)."""
        stmt = (
            select(
                ProductionEvent.work_date,
                Employee.name,
                Style.name,
                SKU.color_code,
                SKU.color_name,
                SKU.size,
                Piece.code,
                Operation.code,
                Operation.label,
                Operation.sequence,
                ProductionEvent.entered_by,
                ClientOrder.order_number,
            )
            .select_from(ProductionEvent)
            .join(Piece, Piece.id == ProductionEvent.piece_id)
            .join(SKU, SKU.id == ProductionEvent.sku_id)
            .join(Style, Style.id == SKU.style_id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
            .join(Operation, Operation.id == ProductionEvent.operation_id)
            .join(Employee, Employee.id == ProductionEvent.employee_id)
        )
        if piece_code:
            stmt = stmt.where(Piece.code == piece_code.strip().upper())
        if employee_id:
            stmt = stmt.where(ProductionEvent.employee_id == employee_id)
        if operation_id:
            stmt = stmt.where(ProductionEvent.operation_id == operation_id)
        if style_id:
            stmt = stmt.where(SKU.style_id == style_id)
        if order_id:
            stmt = stmt.where(Style.client_order_id == order_id)
        if start:
            stmt = stmt.where(ProductionEvent.work_date >= start)
        if end:
            stmt = stmt.where(ProductionEvent.work_date <= end)
        stmt = stmt.order_by(
            ProductionEvent.work_date.desc(), Operation.sequence
        ).limit(limit)

        rows = (await self.db.execute(stmt)).all()
        out: list[dict] = []
        for (work_date, emp_name, style_name, color_code, color_name, size,
             code, op_code, op_label, _seq, entered_by, order_number) in rows:
            out.append({
                "work_date": work_date.isoformat(),
                "employee_name": emp_name,
                "sku_label": sku_label(style_name, color_name, color_code, size),
                "bundle_id": code,            # the piece code (your "bundle id")
                "stage_code": op_code,
                "stage_label": op_label,
                "order_number": order_number,
                "entered_by": entered_by,
            })
        return out

    async def piece_history(self, code: str) -> dict:
        """One piece's ordered stage traveler — every stage it has passed."""
        stages = await self.production_feed(piece_code=code, limit=200)
        # feed is date-desc; present the traveler forward by stage sequence.
        stages = sorted(stages, key=lambda r: (r["work_date"], r["stage_code"]))
        return {
            "bundle_id": code.strip().upper(),
            "sku_label": stages[0]["sku_label"] if stages else None,
            "stages": stages,
        }

    # --------------------------------------------------- existing analytics
    async def stage_spread_alerts(self) -> list[dict]:
        """Detect bottlenecks: where a downstream stage lags CUTTING badly.
        Under per-piece the gap is true WIP-in-flight (pieces cut but not yet
        reached stage X), not miscount noise."""
        alerts: list[dict] = []
        styles = (await self.db.execute(select(Style))).scalars().all()
        for style in styles:
            rows = (await self.db.execute(
                select(Operation.code, func.coalesce(func.sum(ProductionEvent.qty), 0))
                .select_from(ProductionEvent)
                .join(SKU, SKU.id == ProductionEvent.sku_id)
                .join(Operation, Operation.id == ProductionEvent.operation_id)
                .where(SKU.style_id == style.id)
                .group_by(Operation.code)
            )).all()
            totals = dict(rows)
            if not totals:
                continue
            first = totals.get("CUTTING", 0)
            for code, qty in totals.items():
                if code == "CUTTING":
                    continue
                gap = first - int(qty)
                if first > 0 and gap > 0 and gap / first > 0.5:
                    alerts.append({
                        "style": style.name, "stage": code, "cut": first,
                        "reached_stage": int(qty), "gap": gap,
                        "severity": "high" if gap / first > 0.75 else "medium",
                    })
        return alerts

    async def freight_risk(self, today: date | None = None) -> list[dict]:
        """Flag POs approaching their sea-freight cutoff while still in production.
        Missing the sea window forces air freight, which can erase the margin."""
        today = today or date.today()
        warn_from = settings.sea_cutoff_warning_days
        risks: list[dict] = []
        orders = (await self.db.execute(select(ClientOrder))).scalars().all()
        last_seq = await self.db.scalar(select(func.max(Operation.sequence)))
        for order in orders:
            if not order.sea_cutoff_date or order.ship_mode == ShipMode.AIR.value:
                continue
            days_left = (order.sea_cutoff_date - today).days
            if days_left > warn_from:
                continue
            ordered = await self.db.scalar(
                select(func.coalesce(func.sum(SKU.qty_ordered), 0))
                .join(Style, Style.id == SKU.style_id)
                .where(Style.client_order_id == order.id)
            ) or 0
            finished = await self.db.scalar(
                select(func.coalesce(func.sum(ProductionEvent.qty), 0))
                .select_from(ProductionEvent)
                .join(SKU, SKU.id == ProductionEvent.sku_id)
                .join(Style, Style.id == SKU.style_id)
                .join(Operation, Operation.id == ProductionEvent.operation_id)
                .where(Style.client_order_id == order.id, Operation.sequence == last_seq)
            ) or 0
            pct = (finished / ordered) if ordered else 0
            risks.append({
                "order_number": order.order_number,
                "sea_cutoff": order.sea_cutoff_date.isoformat(),
                "days_left": days_left,
                "ordered": int(ordered),
                "finished": int(finished),
                "pct_complete": round(pct * 100, 1),
                "risk": "critical" if days_left <= 2 and pct < 0.9 else
                        "high" if pct < 0.7 else "watch",
            })
        return risks