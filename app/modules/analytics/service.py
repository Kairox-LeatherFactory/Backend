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
from app.modules.analytics.barcode_ext import BarcodeAnalyticsMixin
from app.modules.clients.models import SKU, Client, ClientOrder, Style
from app.modules.clients.service import sku_label
from app.modules.employees.models import Employee
from app.modules.production.models import Operation, Piece, ProductionEvent


def _norm(code: str | None) -> str:
    return (code or "").strip().upper()


class AnalyticsService(BarcodeAnalyticsMixin):
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
        
    # =============================================== explorer navigation tree
    async def explorer_tree(self, *, client_id: uuid.UUID | None = None,
                            include_pieces: bool = True) -> dict:
        """Full Client -> Order -> Style -> Piece nav tree for the left explorer.
        Scoped to one client (CLIENT-role users) or all clients. Piece leaves are
        LIGHT — enough to render + click through to /pieces/detail; full history
        stays in the drill-downs. Built with 4 bounded queries, no N+1.
        Set include_pieces=False at scale to get the skeleton (styles + counts
        only) and lazy-load pieces on style-expand."""
        cstmt = select(Client.id, Client.name).order_by(Client.name)
        if client_id:
            cstmt = cstmt.where(Client.id == client_id)
        clients = (await self.db.execute(cstmt)).all()
        if not clients:
            return {"clients": []}
        client_ids = [c[0] for c in clients]

        orders = (await self.db.execute(
            select(ClientOrder.id, ClientOrder.client_id, ClientOrder.order_number)
            .where(ClientOrder.client_id.in_(client_ids))
            .order_by(ClientOrder.order_number)
        )).all()
        order_ids = [o[0] for o in orders]

        styles = []
        if order_ids:
            styles = (await self.db.execute(
                select(Style.id, Style.client_order_id, Style.name, Style.article)
                .where(Style.client_order_id.in_(order_ids))
                .order_by(Style.name)
            )).all()
        style_ids = [s[0] for s in styles]

        pieces_by_style: dict[uuid.UUID, list[dict]] = {}
        count_by_style: dict[uuid.UUID, int] = {}
        if style_ids:
            if include_pieces:
                rows = (await self.db.execute(
                    select(Piece.id, SKU.style_id, Piece.code, Piece.seq,
                           SKU.color_name, SKU.color_code, SKU.size, Operation.code)
                    .select_from(Piece)
                    .join(SKU, SKU.id == Piece.sku_id)
                    .outerjoin(Operation, Operation.id == Piece.current_operation_id)
                    .where(SKU.style_id.in_(style_ids))
                    .order_by(SKU.code, Piece.seq)
                )).all()
                for pid, sid, code, seq, cname, ccode, size, stage in rows:
                    pieces_by_style.setdefault(sid, []).append({
                        "piece_id": str(pid),
                        "piece_code": code,
                        "seq": seq,
                        "colour": cname or ccode,
                        "size": size,
                        "current_stage": stage,
                    })
                count_by_style = {sid: len(v) for sid, v in pieces_by_style.items()}
            else:
                crows = (await self.db.execute(
                    select(SKU.style_id, func.count(Piece.id))
                    .select_from(Piece)
                    .join(SKU, SKU.id == Piece.sku_id)
                    .where(SKU.style_id.in_(style_ids))
                    .group_by(SKU.style_id)
                )).all()
                count_by_style = {sid: int(n) for sid, n in crows}

        styles_by_order: dict[uuid.UUID, list[dict]] = {}
        for sid, oid, name, article in styles:
            styles_by_order.setdefault(oid, []).append({
                "style_id": str(sid),
                "style_name": name,
                "article": article,
                "piece_count": count_by_style.get(sid, 0),
                "pieces": pieces_by_style.get(sid, []),
            })

        orders_by_client: dict[uuid.UUID, list[dict]] = {}
        for oid, cid, onum in orders:
            ostyles = styles_by_order.get(oid, [])
            orders_by_client.setdefault(cid, []).append({
                "order_id": str(oid),
                "order_number": onum,
                "style_count": len(ostyles),
                "piece_count": sum(s["piece_count"] for s in ostyles),
                "styles": ostyles,
            })

        return {
            "clients": [
                {
                    "client_id": str(cid),
                    "client_name": cname,
                    "order_count": len(orders_by_client.get(cid, [])),
                    "orders": orders_by_client.get(cid, []),
                }
                for cid, cname in clients
            ],
        }

    # ================================================================ LEVEL 1
    async def order_tree(self, order_id: uuid.UUID,
                         *, client_scope: uuid.UUID | None = None) -> dict:
        """Order landing view: the order + its styles, each summarised by piece
        count and how those pieces are distributed across current stages."""
        stmt = (
            select(ClientOrder.order_number, Client.name, Client.id, ClientOrder.id)
            .join(Client, Client.id == ClientOrder.client_id)
            .where(ClientOrder.id == order_id)
        )
        # F33: a CLIENT caller may only read their own order. Add the predicate so
        # a substituted competitor id resolves to 404, not their data.
        if client_scope is not None:
            stmt = stmt.where(ClientOrder.client_id == client_scope)
        head = (await self.db.execute(stmt)).first()
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
    async def style_detail(self, style_id: uuid.UUID,
                           *, client_scope: uuid.UUID | None = None) -> dict:
        """One style with all its pieces, each carrying its full stage history."""
        stmt = (
            select(Style.name, Style.article, ClientOrder.order_number, Client.name)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
            .join(Client, Client.id == ClientOrder.client_id)
            .where(Style.id == style_id)
        )
        if client_scope is not None:      # F33
            stmt = stmt.where(ClientOrder.client_id == client_scope)
        head = (await self.db.execute(stmt)).first()
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
                           seq: int | None = None,
                           client_scope: uuid.UUID | None = None) -> dict:
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
        if client_scope is not None:      # F33
            q = q.where(ClientOrder.client_id == client_scope)
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
    async def stage_spread_alerts(self,
                                  *, client_scope: uuid.UUID | None = None) -> list[dict]:
        """Bottlenecks: where a downstream stage lags the leather cut. Under
        per-piece the gap is true WIP-in-flight (cut but not yet reached stage X)."""
        from app.core.enums import ProductionStage
        cut_code = ProductionStage.LEATHER_CUTTING.value   # F02: not "CUTTING"
        alerts: list[dict] = []
        sstmt = select(Style)
        if client_scope is not None:      # F33: only this client's styles
            sstmt = (sstmt.join(ClientOrder, ClientOrder.id == Style.client_order_id)
                     .where(ClientOrder.client_id == client_scope))
        styles = (await self.db.execute(sstmt)).scalars().all()
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
            first = totals.get(cut_code, 0)
            for code, qty in totals.items():
                if code == cut_code:
                    continue
                gap = first - int(qty)
                if first > 0 and gap > 0 and gap / first > 0.5:
                    alerts.append({
                        "style": style.name, "stage": code, "cut": first,
                        "reached_stage": int(qty), "gap": gap,
                        "severity": "high" if gap / first > 0.75 else "medium",
                    })
        return alerts

    async def freight_risk(self, today: date | None = None,
                           *, client_scope: uuid.UUID | None = None) -> list[dict]:
        today = today or date.today()
        warn_from = settings.sea_cutoff_warning_days
        risks: list[dict] = []
        ostmt = select(ClientOrder)
        if client_scope is not None:      # F33
            ostmt = ostmt.where(ClientOrder.client_id == client_scope)
        orders = (await self.db.execute(ostmt)).scalars().all()
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
    
    
    # ═══════════════════════════════════════════════════ employee rate analytics
    async def employee_rate_analytics(
        self, *, start: date, end: date,
        employee_id: uuid.UUID | None = None,
        style_code: str | None = None,
    ) -> dict:
        """Per-employee earnings analytics: pieces, per-piece rate, and totals,
        broken down by style and operation.

        LIVE, NOT FROZEN — and that distinction matters.
            This reads production_event x rate, so it answers 'what is this
            worker earning RIGHT NOW, mid-period'. It is a management view.
            It is NOT payroll: payroll is wage_line, frozen at run time, and the
            two will legitimately disagree the moment a rate is edited
            mid-period. Never pay from this endpoint; use GET /wages/runs/{id}.

        Rates are resolved per (style, operation, work_date) so a mid-period rate
        change prices each day's work correctly — identical semantics to
        compute_run, deliberately, so the two do not drift.
        """
        if end < start:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "end is before start")

        from app.modules.wages.models import Rate

        # Daily grain so each day is priced at that day's rate.
        stmt = (
            select(
                ProductionEvent.employee_id,
                Employee.name,
                Employee.designation,
                Employee.wage_type,
                Style.id,
                Style.code,
                Style.name,
                Operation.id,
                Operation.code,
                Operation.label,
                ProductionEvent.work_date,
                func.sum(ProductionEvent.qty),
            )
            .select_from(ProductionEvent)
            .join(Employee, Employee.id == ProductionEvent.employee_id)
            .join(SKU, SKU.id == ProductionEvent.sku_id)
            .join(Style, Style.id == SKU.style_id)
            .join(Operation, Operation.id == ProductionEvent.operation_id)
            .where(ProductionEvent.work_date >= start,
                   ProductionEvent.work_date <= end)
            .group_by(
                ProductionEvent.employee_id, Employee.name, Employee.designation,
                Employee.wage_type, Style.id, Style.code, Style.name,
                Operation.id, Operation.code, Operation.label,
                ProductionEvent.work_date,
            )
        )
        if employee_id:
            stmt = stmt.where(ProductionEvent.employee_id == employee_id)
        if style_code:
            stmt = stmt.where(Style.code == _norm(style_code))

        rows = (await self.db.execute(stmt)).all()
        if not rows:
            return {"start": start, "end": end, "employees": [],
                    "total_pieces": 0, "total_amount": 0.0}

        # Pre-load every rate that could apply, ONE query, then resolve in Python.
        # The alternative — a scalar subquery per row — is an N-query payroll
        # report, and this endpoint is the one a manager refreshes all day.
        pairs = {(r[4], r[7]) for r in rows}
        rate_rows = (await self.db.execute(
            select(Rate.style_id, Rate.operation_id, Rate.rate, Rate.effective_from)
            .where(Rate.style_id.in_({p[0] for p in pairs}),
                   Rate.operation_id.in_({p[1] for p in pairs}),
                   Rate.effective_from <= end)
            .order_by(Rate.effective_from)
        )).all()
        rate_hist: dict[tuple, list[tuple[date, float]]] = {}
        for sid, oid, rate, eff in rate_rows:
            rate_hist.setdefault((sid, oid), []).append((eff, float(rate)))

        def _rate_on(style_id, op_id, on: date) -> float | None:
            """Latest rate with effective_from <= on. Mirrors
            WageRepository.effective_rate exactly."""
            hist = rate_hist.get((style_id, op_id))
            if not hist:
                return None
            picked = None
            for eff, val in hist:          # ascending
                if eff <= on:
                    picked = val
                else:
                    break
                    
            return picked

        # (emp_id, style_id, op_id) -> accumulator
        agg: dict[tuple, dict] = {}
        emp_meta: dict[uuid.UUID, dict] = {}
        for (emp_id, emp_name, desig, wage_type, style_id, scode, sname,
             op_id, ocode, olabel, wdate, qty) in rows:
            emp_meta.setdefault(emp_id, {
                "employee_id": str(emp_id), "employee_name": emp_name,
                "designation": desig,
                "wage_type": getattr(wage_type, "value", str(wage_type)),
            })
            rate = _rate_on(style_id, op_id, wdate)
            key = (emp_id, style_id, op_id)
            a = agg.setdefault(key, {
                "style_code": scode, "style_name": sname,
                "operation_code": ocode, "operation_label": olabel,
                "pieces": 0, "amount": 0.0,
                "rates_applied": set(), "unrated_pieces": 0,
            })
            a["pieces"] += int(qty)
            if rate is None:
                # Real output that prices to nothing. Surfaced, never silently
                # treated as zero — that is how a worker opens an empty envelope.
                a["unrated_pieces"] += int(qty)
            else:
                a["amount"] += float(qty) * rate
                a["rates_applied"].add(round(rate, 2))

        by_emp: dict[uuid.UUID, list[dict]] = {}
        for (emp_id, _sid, _oid), a in agg.items():
            rates = sorted(a.pop("rates_applied"))
            by_emp.setdefault(emp_id, []).append({
                **a,
                # Single rate for the period -> show it. Several (a mid-period
                # reprice) -> null plus the list, because no single number is
                # the rate this work was paid at.
                "rate": rates[0] if len(rates) == 1 else None,
                "rates_applied": rates,
                "amount": round(a["amount"], 2),
            })

        employees = []
        for emp_id, lines in by_emp.items():
            lines.sort(key=lambda x: (x["style_code"], x["operation_code"]))
            employees.append({
                **emp_meta[emp_id],
                "total_pieces": sum(x["pieces"] for x in lines),
                "total_amount": round(sum(x["amount"] for x in lines), 2),
                "unrated_pieces": sum(x["unrated_pieces"] for x in lines),
                "lines": lines,
            })
        employees.sort(key=lambda e: -e["total_amount"])

        return {
            "start": start,
            "end": end,
            "employees": employees,
            "total_pieces": sum(e["total_pieces"] for e in employees),
            "total_amount": round(sum(e["total_amount"] for e in employees), 2),
            "note": (
                "Live estimate from production events and current rates. "
                "Payroll of record is GET /wages/runs/{id}."
            ),
        }