"""
================================================================================
modules/analytics/barcode_ext.py — Barcode-feature analytics (read-only)
================================================================================
Two new read paths, delivered as a MIXIN so the large shipped AnalyticsService
does not have to be reproduced. Both are additive and read-only.

MERGE (one edit to modules/analytics/service.py):

    from app.modules.analytics.barcode_ext import BarcodeAnalyticsMixin

    class AnalyticsService(BarcodeAnalyticsMixin):     # add the mixin base
        def __init__(self, db: AsyncSession):
            self.db = db
            ...

The mixin only uses `self.db`, which the service already sets in __init__, so no
other change is needed. Router endpoints are at the bottom (add to
analytics/router.py).

WHY READ-ONLY, AND WHY IT MAY DISAGREE WITH PAYROLL
    These read the production_event log (which now carries consumption). The
    per-piece story is exactly what capture wrote — nothing invented. The
    consumption rollup is a LIVE management view; payroll of record stays the
    frozen wage_run. The two legitimately differ mid-period.
================================================================================
"""
import uuid


class BarcodeAnalyticsMixin:
    """Mixed into AnalyticsService. Uses only self.db."""

    async def piece_life_story(self, piece_code: str,
                               *, client_scope: uuid.UUID | None = None) -> dict:
        """F1 — the garment's whole life: every stage, who did it, when, entered
        by, rework flag, leather consumption at cutting, current stage + wait."""
        from fastapi import HTTPException, status
        from sqlalchemy import func, select

        from app.modules.barcode.models import Drawer
        from app.modules.clients.models import SKU, Client, ClientOrder, Style
        from app.modules.employees.models import Employee
        from app.modules.production.models import Operation, Piece, ProductionEvent

        code = (piece_code or "").strip().upper()
        stmt = (
            select(Piece, SKU, Style, ClientOrder.order_number, Client.name)
            .join(SKU, SKU.id == Piece.sku_id)
            .join(Style, Style.id == SKU.style_id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
            .join(Client, Client.id == ClientOrder.client_id)
            .where(func.upper(Piece.code) == code)
        )
        if client_scope is not None:      # F33
            stmt = stmt.where(ClientOrder.client_id == client_scope)
        head = (await self.db.execute(stmt)).first()
        if not head:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown piece '{piece_code}'.")
        piece, sku, style, order_number, client = head

        rows = (await self.db.execute(
            select(Operation.code, Operation.label, Employee.name,
                   ProductionEvent.work_date, ProductionEvent.created_at,
                   ProductionEvent.entered_by, ProductionEvent.consumption_qty)
            .join(Operation, Operation.id == ProductionEvent.operation_id)
            .outerjoin(Employee, Employee.id == ProductionEvent.employee_id)
            .where(ProductionEvent.piece_id == piece.id)
            .order_by(ProductionEvent.created_at)
        )).all()

        seen: set[str] = set()
        stages = []
        for ocode, olabel, emp_name, wdate, logged_at, entered_by, cons in rows:
            is_rework = ocode in seen
            seen.add(ocode)
            stages.append({
                "stage": ocode, "stage_label": olabel, "employee_name": emp_name,
                "work_date": wdate.isoformat() if wdate else None,
                "logged_at": logged_at.isoformat() if logged_at else None,
                "entered_by": entered_by, "is_rework": is_rework,
                "leather_consumption_dcm": float(cons) if cons is not None else None,
            })

        drawer_code = drawer_state = None
        if getattr(piece, "drawer_id", None):
            d = await self.db.get(Drawer, piece.drawer_id)
            if d:
                drawer_code, drawer_state = d.code, d.state
        current = stages[-1]["stage"] if stages else None
        return {
            "piece_code": piece.code, "style_name": style.name,
            "colour": sku.color_name or sku.color_code, "size": sku.size,
            "order_number": order_number, "client": client,
            "current_stage": current, "drawer_code": drawer_code,
            "drawer_state": drawer_state,
            "awaiting": self._awaiting_text(current, drawer_state,
                                            bool(getattr(piece, "needs_lining", True))),
            "stages": stages,
        }

    @staticmethod
    def _awaiting_text(current, drawer_state, needs_lining):
        if drawer_state == "holding_leather" and needs_lining:
            return "in drawer, awaiting lining"
        if drawer_state == "holding_lining":     # F07: lining stored, leather not yet
            return "in drawer, awaiting leather"
        if drawer_state in ("merged", "waiting"):
            return "awaiting storage"
        if current == "FINAL_INSPECTION":
            return "awaiting inspection sign-off"
        return None

    async def consumption_vs_stock(self, *, order_id=None, style_id=None,
                                   client_scope: uuid.UUID | None = None) -> dict:
        """F4 — leather consumed (summed from cut events) per style + total."""
        from sqlalchemy import func, select

        from app.modules.clients.models import SKU, ClientOrder, Style
        from app.modules.production.models import Operation, Piece, ProductionEvent

        stmt = (
            select(Style.id, Style.name,
                   func.count(func.distinct(Piece.id)),
                   func.coalesce(func.sum(ProductionEvent.consumption_qty), 0))
            .select_from(ProductionEvent)
            .join(Operation, Operation.id == ProductionEvent.operation_id)
            .join(Piece, Piece.id == ProductionEvent.piece_id)
            .join(SKU, SKU.id == Piece.sku_id)
            .join(Style, Style.id == SKU.style_id)
            .where(func.upper(Operation.code) == "LEATHER_CUTTING")
            .group_by(Style.id, Style.name)
        )
        if style_id:
            stmt = stmt.where(Style.id == style_id)
        if order_id:
            stmt = stmt.where(Style.client_order_id == order_id)
        if client_scope is not None:      # F33: only this client's styles
            stmt = (stmt.join(ClientOrder, ClientOrder.id == Style.client_order_id)
                    .where(ClientOrder.client_id == client_scope))

        rows = (await self.db.execute(stmt)).all()
        styles = [{
            "style_id": str(sid), "style_name": name,
            "pieces_cut": int(pc), "leather_consumed_dcm": float(c),
        } for sid, name, pc, c in rows]
        return {
            "styles": styles,
            "total_consumed_dcm": round(sum(s["leather_consumed_dcm"] for s in styles), 3),
        }