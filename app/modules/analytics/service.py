"""
================================================================================
modules/analytics/service.py — Read-only cross-module analytics (async)
================================================================================
Grouped drill-down for the floor UI:
    order_tree(order_id)        order -> styles (each with piece count + current-
                                stage distribution). The landing view.
    style_detail(style_id)      one style -> its pieces, each with its full stage
                                history (employee + date/time per stage, rework
                                flagged). Shown when the user picks a style.
    piece_detail(...)           one piece by piece_code OR sku_code+seq -> order/
                                style/sku header + ordered stage history.

Plus the dashboard/alert helpers. Reads across modules for reporting; never
writes. All rows are built with explicit joins — no lazy access under async.
================================================================================
"""
import uuid
from datetime import date

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.enums import ShipMode
from app.modules.clients.models import SKU, Client, ClientOrder, Style
from app.modules.clients.service import sku_label
from app.modules.employees.models import Employee
from app.modules.production.models import Operation, Piece, ProductionEvent


def _norm(code: str | None) -> str:
    return (code or "").strip().upper()


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

    # ================================================================ LEVEL 1
    async def order_tree(self, order_id: uuid.UUID) -> dict:
        """Order landing view: the order + its styles, each summarised by piece
        count and how those pieces are distributed across current stages."""
        head = (await self.db.execute(
            select(ClientOrder.order_number, Client.name, Client.id, ClientOrder.id)
            .join(Client, Client.id == ClientOrder.client_id)
            .where(ClientOrder.id == order_id)
        )).first()
        if not head:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
        order_number, client_name, _cid, _oid = head

        styles = (await self.db.execute(
            select(Style.id, Style.name, Style.article)
            .where(Style.client_order_id == order_id)
            .order_by(Style.name)
        )).all()

        # Current-stage distribution per style, one grouped query.
        dist = (await self.db.execute(
            select(Style.id, Operation.code, func.count(Piece.id))
            .select_from(Piece)
            .join(SKU, SKU.id == Piece.sku_id)
            .join(Style, Style.id == SKU.style_id)
            .outerjoin(Operation, Operation.id == Piece.current_operation_id)
            .where(Style.client_order_id == order_id)
            .group_by(Style.id, Operation.code)
        )).all()
        by_style: dict[uuid.UUID, dict] = {}
        for style_id, op_code, cnt in dist:
            d = by_style.setdefault(style_id, {"count": 0, "stages": {}})
            d["count"] += int(cnt)
            d["stages"][op_code or "UNSTARTED"] = int(cnt)

        return {
            "order_id": str(order_id),
            "order_number": order_number,
            "client": client_name,
            "styles": [
                {
                    "style_id": str(sid),
                    "style_name": name,
                    "article": article,
                    "piece_count": by_style.get(sid, {}).get("count", 0),
                    "stage_counts": by_style.get(sid, {}).get("stages", {}),
                }
                for sid, name, article in styles
            ],
        }

    # ================================================================ LEVEL 2
    async def style_detail(self, style_id: uuid.UUID) -> dict:
        """One style with all its pieces, each carrying its full stage history."""
        head = (await self.db.execute(
            select(Style.name, Style.article, ClientOrder.order_number, Client.name)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
            .join(Client, Client.id == ClientOrder.client_id)
            .where(Style.id == style_id)
        )).first()
        if not head:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Style not found")
        style_name, article, order_number, client_name = head

        pieces = (await self.db.execute(
            select(Piece.id, Piece.code, Piece.seq, SKU.code,
                   SKU.color_name, SKU.color_code, SKU.size)
            .join(SKU, SKU.id == Piece.sku_id)
            .where(SKU.style_id == style_id)
            .order_by(SKU.code, Piece.seq)
        )).all()

        # All events for this style's pieces in one query; group in Python.
        events = (await self.db.execute(
            select(ProductionEvent.piece_id, Operation.code, Operation.label,
                   Operation.sequence, Employee.name, ProductionEvent.work_date,
                   ProductionEvent.entered_by, ProductionEvent.created_at)
            .select_from(ProductionEvent)
            .join(Piece, Piece.id == ProductionEvent.piece_id)
            .join(SKU, SKU.id == Piece.sku_id)
            .join(Operation, Operation.id == ProductionEvent.operation_id)
            .join(Employee, Employee.id == ProductionEvent.employee_id)
            .where(SKU.style_id == style_id)
            .order_by(ProductionEvent.piece_id, ProductionEvent.work_date,
                      ProductionEvent.created_at)
        )).all()

        stages_by_piece: dict[uuid.UUID, list[dict]] = {}
        seen_ops: dict[uuid.UUID, set] = {}
        for (pid, op_code, op_label, _seq, emp, wdate, entered_by, created) in events:
            seen = seen_ops.setdefault(pid, set())
            stages_by_piece.setdefault(pid, []).append({
                "stage_code": op_code,
                "stage_label": op_label,
                "employee_name": emp,
                "work_date": wdate.isoformat() if wdate else None,
                "logged_at": created.isoformat() if created else None,
                "entered_by": entered_by,
                "is_rework": op_code in seen,
            })
            seen.add(op_code)

        return {
            "style_id": str(style_id),
            "style_name": style_name,
            "article": article,
            "order_number": order_number,
            "client": client_name,
            "pieces": [
                {
                    "piece_id": str(pid),
                    "bundle_id": code,
                    "seq": seq,
                    "sku_code": sku_code,
                    "colour": color_name or color_code,
                    "size": size,
                    "current_stage": (stages_by_piece.get(pid) or [{}])[-1].get("stage_code"),
                    "stages": stages_by_piece.get(pid, []),
                }
                for (pid, code, seq, sku_code, color_name, color_code, size) in pieces
            ],
        }

    # ================================================================ LEVEL 3
    async def piece_detail(self, *, piece_code: str | None = None,
                           sku_code: str | None = None,
                           seq: int | None = None) -> dict:
        """One piece by piece_code OR (sku_code + seq): header + stage history."""
        q = (
            select(Piece.id, Piece.code, Piece.seq,
                   SKU.code, SKU.color_name, SKU.color_code, SKU.size,
                   Style.name, Style.article,
                   ClientOrder.order_number, Client.name)
            .select_from(Piece)
            .join(SKU, SKU.id == Piece.sku_id)
            .join(Style, Style.id == SKU.style_id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
            .join(Client, Client.id == ClientOrder.client_id)
        )
        if piece_code:
            q = q.where(Piece.code == _norm(piece_code))
        elif sku_code and seq is not None:
            q = q.where(SKU.code == _norm(sku_code), Piece.seq == seq)
        else:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "Provide piece_code or (sku_code + seq).")
        row = (await self.db.execute(q)).first()
        if not row:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Piece not found")
        (pid, code, pseq, skucode, color_name, color_code, size,
         style_name, article, order_number, client_name) = row

        ev = (await self.db.execute(
            select(Operation.code, Operation.label, Operation.sequence,
                   Employee.name, ProductionEvent.work_date,
                   ProductionEvent.entered_by, ProductionEvent.created_at)
            .select_from(ProductionEvent)
            .join(Operation, Operation.id == ProductionEvent.operation_id)
            .join(Employee, Employee.id == ProductionEvent.employee_id)
            .where(ProductionEvent.piece_id == pid)
            .order_by(ProductionEvent.work_date, ProductionEvent.created_at)
        )).all()
        stages: list[dict] = []
        seen: set = set()
        for (op_code, op_label, _s, emp, wdate, entered_by, created) in ev:
            stages.append({
                "stage_code": op_code,
                "stage_label": op_label,
                "employee_name": emp,
                "work_date": wdate.isoformat() if wdate else None,
                "logged_at": created.isoformat() if created else None,
                "entered_by": entered_by,
                "is_rework": op_code in seen,
            })
            seen.add(op_code)

        return {
            "bundle_id": code,
            "seq": pseq,
            "sku_code": skucode,
            "sku_label": sku_label(style_name, color_name, color_code, size),
            "colour": color_name or color_code,
            "size": size,
            "style_name": style_name,
            "article": article,
            "order_number": order_number,
            "client": client_name,
            "current_stage": stages[-1]["stage_code"] if stages else None,
            "stages": stages,
        }

    # =============================================================== dashboards
    async def stage_spread_alerts(self) -> list[dict]:
        """Bottlenecks: where a downstream stage lags CUTTING. Under per-piece the
        gap is true WIP-in-flight (cut but not yet reached stage X)."""
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