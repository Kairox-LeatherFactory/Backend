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
from app.core.enums import DrawerState, ProductionStage, ShipMode
from app.core.store_display import display_stage, holding_label
from app.modules.analytics.barcode_ext import BarcodeAnalyticsMixin
from app.modules.clients.models import SKU, Client, ClientOrder, Style
from app.modules.clients.service import sku_label
from app.modules.employees.models import Employee
from app.modules.production.models import Operation, Piece, ProductionEvent


def _norm(code: str | None) -> str:
    return (code or "").strip().upper()


# ══════════════════════════════════════════════════════════════════════════════
# THE STAGE SPREADSHEET — shared scope helpers (change-list item 2 / your item 4)
# ══════════════════════════════════════════════════════════════════════════════
# The order / style / piece drill-down asks the SAME three questions at every
# level, and they are only comparable if all three levels compute them the same
# way:
#
#   1. how much was ordered, how much is finished, how much is left
#   2. per stage, how many pieces have cleared it — out of how many
#   3. where the pieces physically are in the store right now
#
# So they live in one place, parameterised by scope, rather than being written
# three times. Three copies of "completed" is how an order page and a style page
# end up disagreeing about the same garments, which makes both unusable.
#
# EVERY STAGE IS REPORTED, including ones with zero events. A funnel that omits
# empty stages silently renames the pipeline depending on how far the order has
# got, and "FINAL_FINISH is missing" reads as a bug rather than as "nothing has
# reached final finish yet".
_PIPELINE_STAGES: list[str] = [
    ProductionStage.LEATHER_CUTTING.value,
    ProductionStage.LINING_CUTTING.value,
    ProductionStage.FUSING.value,
    ProductionStage.PASTING.value,
    ProductionStage.LINE_STITCHING.value,
    ProductionStage.SHELL_STITCHING.value,
    ProductionStage.FINAL_FINISH.value,
    ProductionStage.FINAL_INSPECTION.value,
    ProductionStage.PACKAGE_EXPORT.value,
]
_TERMINAL_STAGE = ProductionStage.PACKAGE_EXPORT.value

# Drawer states, grouped as the store screen speaks about them. `no_drawer` is
# not a drawer state at all — it is a piece with `drawer_id IS NULL`, i.e. one
# minted while the pool was full. It belongs in this block because from the
# floor's point of view "where is it in the store" and "it is not in the store,
# and here is why" are the same question.
_STORE_BUCKETS: list[tuple[str, str]] = [
    (DrawerState.MERGED.value, "Assigned, nothing scanned in"),
    (DrawerState.HOLDING_LEATHER.value, "Holding leather"),
    (DrawerState.HOLDING_LINING.value, "Holding lining"),
    (DrawerState.HOLDING_BOTH.value, "Holding both"),
    (DrawerState.RECEIVED.value, "Received, ready to send"),
    (DrawerState.SENDED.value, "Sent to line-stitching"),
    (DrawerState.WAITING.value, "Drawer free (piece shipped)"),
]


class AnalyticsService(BarcodeAnalyticsMixin):
    def __init__(self, db: AsyncSession):
        self.db = db

    # ── scope plumbing ───────────────────────────────────────────────────────
    @staticmethod
    def _apply_scope(stmt, *, order_id=None, style_id=None, client_scope=None):
        """Narrow a Piece→SKU→Style→ClientOrder statement to one order or style.

        One helper so the order level and the style level cannot drift into
        filtering on different things — the style page must be a strict subset of
        the order page, and it is only guaranteed to be if the predicate is
        literally the same code.
        """
        if style_id is not None:
            stmt = stmt.where(Style.id == style_id)
        if order_id is not None:
            stmt = stmt.where(Style.client_order_id == order_id)
        if client_scope is not None:
            stmt = stmt.where(ClientOrder.client_id == client_scope)
        return stmt

    async def _scope_totals(self, *, order_id=None, style_id=None,
                            client_scope=None) -> dict:
        """Ordered / minted / completed / balance for an order or a style.

        COMPLETED MEANS THE GARMENT IS FINISHED — it has a PACKAGE_EXPORT event.
        Not "has reached some stage", not "its current_operation_id is set".
        A piece cut but awaiting fusing is NOT completed; it is part of the
        balance, which is exactly what the floor means by pending.

        ORDERED vs MINTED are two different denominators and both are reported.
        `ordered` is what the client asked for (SUM of SKU.qty_ordered);
        `minted` is how many barcoded garments actually exist. They differ
        whenever a style is still DRAFT (nothing minted) or was released
        partially, and showing only one of them makes an unreleased style look
        either finished or missing.
        """
        ordered_stmt = (
            select(func.coalesce(func.sum(SKU.qty_ordered), 0))
            .select_from(SKU)
            .join(Style, Style.id == SKU.style_id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
        )
        ordered = int(await self.db.scalar(self._apply_scope(
            ordered_stmt, order_id=order_id, style_id=style_id,
            client_scope=client_scope)) or 0)

        minted_stmt = (
            select(func.count(func.distinct(Piece.id)))
            .select_from(Piece)
            .join(SKU, SKU.id == Piece.sku_id)
            .join(Style, Style.id == SKU.style_id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
        )
        minted = int(await self.db.scalar(self._apply_scope(
            minted_stmt, order_id=order_id, style_id=style_id,
            client_scope=client_scope)) or 0)

        done_stmt = (
            select(func.count(func.distinct(ProductionEvent.piece_id)))
            .select_from(ProductionEvent)
            .join(Operation, Operation.id == ProductionEvent.operation_id)
            .join(Piece, Piece.id == ProductionEvent.piece_id)
            .join(SKU, SKU.id == Piece.sku_id)
            .join(Style, Style.id == SKU.style_id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
            .where(func.upper(Operation.code) == _TERMINAL_STAGE)
        )
        completed = int(await self.db.scalar(self._apply_scope(
            done_stmt, order_id=order_id, style_id=style_id,
            client_scope=client_scope)) or 0)

        return {
            "qty_ordered": ordered,
            "minted_pieces": minted,
            "completed": completed,
            # THE BALANCE IS AGAINST WHAT WAS ORDERED, not against what was
            # minted: a style nobody has released yet is still owed to the
            # client, and a balance that ignored it would report an order as
            # complete while half of it had not been started.
            "balance": max(ordered - completed, 0),
            "completion_pct": round(completed / ordered * 100, 1) if ordered else 0.0,
            "not_minted": max(ordered - minted, 0),
        }

    async def _stage_progress(self, *, total: int, order_id=None, style_id=None,
                              client_scope=None) -> list[dict]:
        """Per stage: how many distinct pieces have cleared it, out of `total`.

        `pending` is the BALANCE at that stage — total minus completed — which is
        what the floor means: a piece that has finished cutting and is waiting for
        fusing is pending AT FUSING. It is deliberately not "queue depth"
        (upstream completed minus this stage's completed): every stage then has a
        different denominator and the columns stop adding up against the order.

        ONE grouped query for every stage, not one per stage.
        """
        stmt = (
            select(func.upper(Operation.code),
                   func.count(func.distinct(ProductionEvent.piece_id)))
            .select_from(ProductionEvent)
            .join(Operation, Operation.id == ProductionEvent.operation_id)
            .join(Piece, Piece.id == ProductionEvent.piece_id)
            .join(SKU, SKU.id == Piece.sku_id)
            .join(Style, Style.id == SKU.style_id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
            .group_by(func.upper(Operation.code))
        )
        rows = (await self.db.execute(self._apply_scope(
            stmt, order_id=order_id, style_id=style_id,
            client_scope=client_scope))).all()
        done = {code: int(n or 0) for code, n in rows}

        return [{
            "stage": code,
            "label": code.replace("_", " ").title(),
            "total": total,
            "completed": done.get(code, 0),
            "pending": max(total - done.get(code, 0), 0),
            "pct": round(done.get(code, 0) / total * 100, 1) if total else 0.0,
        } for code in _PIPELINE_STAGES]

    async def _store_block(self, *, order_id=None, style_id=None,
                           client_scope=None) -> dict:
        """Where this scope's pieces are in the store, by drawer state.

        Answers "how many are holding leather / lining / both / sent" for an
        order or a style — the part of the drill-down that no existing endpoint
        served, and the one the floor asks about most, because a piece sitting in
        a drawer is invisible on a stage funnel: it has cleared its cut and has
        no new event until line-stitching, so a pure event view shows it parked
        at PASTING with no indication that it is actually waiting on its lining.
        """
        from app.modules.barcode.models import Drawer

        stmt = (
            select(Drawer.state, func.count(func.distinct(Piece.id)))
            .select_from(Piece)
            .join(SKU, SKU.id == Piece.sku_id)
            .join(Style, Style.id == SKU.style_id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
            .join(Drawer, Drawer.id == Piece.drawer_id)
            .group_by(Drawer.state)
        )
        rows = (await self.db.execute(self._apply_scope(
            stmt, order_id=order_id, style_id=style_id,
            client_scope=client_scope))).all()
        by_state = {(s or "").lower(): int(n or 0) for s, n in rows}

        # Pieces with no drawer at all — minted while the pool was full. They
        # have barcodes and cannot be stored, so they are stalled in a way no
        # drawer state can express.
        nod_stmt = (
            select(func.count(func.distinct(Piece.id)))
            .select_from(Piece)
            .join(SKU, SKU.id == Piece.sku_id)
            .join(Style, Style.id == SKU.style_id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
            .where(Piece.drawer_id.is_(None))
        )
        no_drawer = int(await self.db.scalar(self._apply_scope(
            nod_stmt, order_id=order_id, style_id=style_id,
            client_scope=client_scope)) or 0)

        buckets = [{"state": state, "label": label,
                    "pieces": by_state.get(state, 0)}
                   for state, label in _STORE_BUCKETS]
        holding = (by_state.get(DrawerState.HOLDING_LEATHER.value, 0)
                   + by_state.get(DrawerState.HOLDING_LINING.value, 0)
                   + by_state.get(DrawerState.HOLDING_BOTH.value, 0))
        return {
            "buckets": buckets,
            "holding_leather": by_state.get(DrawerState.HOLDING_LEATHER.value, 0),
            "holding_lining": by_state.get(DrawerState.HOLDING_LINING.value, 0),
            "holding_both": by_state.get(DrawerState.HOLDING_BOTH.value, 0),
            "received": by_state.get(DrawerState.RECEIVED.value, 0),
            "sended": by_state.get(DrawerState.SENDED.value, 0),
            "in_store": holding + by_state.get(DrawerState.RECEIVED.value, 0),
            "awaiting_parts": by_state.get(DrawerState.MERGED.value, 0),
            "no_drawer": no_drawer,
        }

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
        """ORDER LEVEL of the stage spreadsheet.

        The order's own totals (ordered / completed / balance), its per-stage
        progress (how many pieces have cleared each stage, out of the whole
        order), where its garments are sitting in the store, and the style list
        to drill into next.

        `totals` and `stages` are computed by the same helpers the style level
        uses, so a style page is always a strict subset of this one. `styles[]`
        carries `style_id` + `style_code` because the next click needs both — the
        id to fetch, the code for a human to recognise.
        """
        stmt = (
            select(ClientOrder.order_number, Client.name, Client.id, ClientOrder.id,
                   ClientOrder.delivery_deadline)
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
        order_number, client_name, _cid, _oid, deadline = head

        styles = (await self.db.execute(
            select(Style.id, Style.name, Style.article, Style.code,
                   Style.production_status, Style.needs_lining)
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

        # Per-style ordered + completed, two grouped queries for the whole order
        # rather than _scope_totals per style (which would be 2N round trips on an
        # order with 17 styles).
        ordered_rows = (await self.db.execute(
            select(SKU.style_id, func.coalesce(func.sum(SKU.qty_ordered), 0))
            .select_from(SKU)
            .join(Style, Style.id == SKU.style_id)
            .where(Style.client_order_id == order_id)
            .group_by(SKU.style_id)
        )).all()
        ordered_by_style = {sid: int(n or 0) for sid, n in ordered_rows}

        done_rows = (await self.db.execute(
            select(SKU.style_id, func.count(func.distinct(ProductionEvent.piece_id)))
            .select_from(ProductionEvent)
            .join(Operation, Operation.id == ProductionEvent.operation_id)
            .join(Piece, Piece.id == ProductionEvent.piece_id)
            .join(SKU, SKU.id == Piece.sku_id)
            .join(Style, Style.id == SKU.style_id)
            .where(Style.client_order_id == order_id,
                   func.upper(Operation.code) == _TERMINAL_STAGE)
            .group_by(SKU.style_id)
        )).all()
        done_by_style = {sid: int(n or 0) for sid, n in done_rows}

        totals = await self._scope_totals(order_id=order_id,
                                          client_scope=client_scope)
        stages = await self._stage_progress(
            total=totals["qty_ordered"], order_id=order_id,
            client_scope=client_scope)
        store = await self._store_block(order_id=order_id,
                                        client_scope=client_scope)

        style_rows = []
        for sid, name, article, code, prod_status, needs_lining in styles:
            ordered = ordered_by_style.get(sid, 0)
            completed = done_by_style.get(sid, 0)
            style_rows.append({
                "style_id": str(sid),
                "style_code": code,
                "style_name": name,
                "article": article,
                "production_status": prod_status,
                "needs_lining": needs_lining,
                "qty_ordered": ordered,
                "completed": completed,
                "balance": max(ordered - completed, 0),
                "completion_pct": (round(completed / ordered * 100, 1)
                                   if ordered else 0.0),
                "piece_count": by_style.get(sid, {}).get("count", 0),
                "stage_counts": by_style.get(sid, {}).get("stages", {}),
            })

        return {
            "order_id": str(order_id),
            "order_number": order_number,
            "client": client_name,
            "delivery_deadline": deadline.isoformat() if deadline else None,
            # Ordered / minted / completed / balance for the whole order.
            "totals": totals,
            # Per-stage "X of Y done, Z pending" across the whole order.
            "stages": stages,
            # Where the order's garments are physically sitting right now.
            "store": store,
            "style_count": len(style_rows),
            "styles": style_rows,
        }

    # ================================================================ LEVEL 2
    async def style_detail(self, style_id: uuid.UUID,
                           *, client_scope: uuid.UUID | None = None) -> dict:
        """One style with all its pieces, each carrying its full stage history."""
        stmt = (
            select(Style.name, Style.article, ClientOrder.order_number, Client.name,
                   Style.code, Style.production_status, Style.needs_lining,
                   ClientOrder.id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
            .join(Client, Client.id == ClientOrder.client_id)
            .where(Style.id == style_id)
        )
        if client_scope is not None:      # F33
            stmt = stmt.where(ClientOrder.client_id == client_scope)
        head = (await self.db.execute(stmt)).first()
        if not head:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Style not found")
        (style_name, article, order_number, client_name, style_code,
         prod_status, needs_lining, order_id) = head

        # The piece list carries its drawer, so a row can show STORE without a
        # second call. OUTER join: a piece with no drawer (minted while the pool
        # was full) must still appear — it is precisely the row someone is
        # looking for when they ask why a style has stalled.
        from app.modules.barcode.models import Drawer

        pieces = (await self.db.execute(
            select(Piece.id, Piece.code, Piece.seq, SKU.code,
                   SKU.color_name, SKU.color_code, SKU.size,
                   Drawer.code, Drawer.state, Drawer.leather_in, Drawer.lining_in)
            .join(SKU, SKU.id == Piece.sku_id)
            .outerjoin(Drawer, Drawer.id == Piece.drawer_id)
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

        totals = await self._scope_totals(style_id=style_id,
                                          client_scope=client_scope)
        stage_rows = await self._stage_progress(
            total=totals["qty_ordered"], style_id=style_id,
            client_scope=client_scope)
        store = await self._store_block(style_id=style_id,
                                        client_scope=client_scope)

        piece_rows = []
        for (pid, code, seq, sku_code, color_name, color_code, size,
             drawer_code, drawer_state, leather_in, lining_in) in pieces:
            history = stages_by_piece.get(pid, [])
            event_stage = history[-1]["stage_code"] if history else None
            # The STORE overlay, same rule the production screens apply: a piece
            # parked in a drawer shows STORE rather than the cut stage it last
            # logged, because "PASTING" on a garment that has been sitting in the
            # store for three days is a true statement that answers the wrong
            # question.
            disp = display_stage(current_event_stage=event_stage,
                                 drawer_state=drawer_state,
                                 needs_lining=bool(needs_lining)
                                 if needs_lining is not None else True)
            piece_rows.append({
                "piece_id": str(pid),
                "bundle_id": code,
                "piece_code": code,
                "seq": seq,
                "serial": f"{seq:03d}" if seq is not None else None,
                "sku_code": sku_code,
                "colour": color_name or color_code,
                "size": size,
                # The real event-backed stage, kept distinct from what the board
                # shows — two different questions, and conflating them is how a
                # piece appears to be at LINE_STITCHING before it is stitched.
                "current_stage": event_stage,
                "display_stage": disp["display_stage"],
                "display_label": disp["label"],
                "in_store": disp["in_store"],
                "store_status": disp["store_status"],
                "drawer_code": drawer_code,
                "drawer_state": drawer_state,
                "holding": (holding_label(leather_in=leather_in,
                                          lining_in=lining_in)
                            if drawer_code else None),
                "completed": any(s["stage_code"] == _TERMINAL_STAGE
                                 for s in history),
                "stages": history,
            })

        return {
            "style_id": str(style_id),
            "style_code": style_code,
            "style_name": style_name,
            "article": article,
            "order_id": str(order_id),
            "order_number": order_number,
            "client": client_name,
            "production_status": prod_status,
            "needs_lining": needs_lining,
            "totals": totals,
            "stages": stage_rows,
            "store": store,
            "piece_count": len(piece_rows),
            "pieces": piece_rows,
        }

    # ================================================================ LEVEL 3
    async def piece_detail(self, *, piece_code: str | None = None,
                           sku_code: str | None = None,
                           seq: int | None = None,
                           client_scope: uuid.UUID | None = None) -> dict:
        """One piece by piece_code OR (sku_code + seq): header + stage history."""
        from app.modules.barcode.models import Drawer

        q = (
            select(Piece.id, Piece.code, Piece.seq,
                   SKU.code, SKU.color_name, SKU.color_code, SKU.size,
                   Style.name, Style.article,
                   ClientOrder.order_number, Client.name,
                   Style.id, ClientOrder.id, Style.code, Style.needs_lining,
                   Piece.needs_lining,
                   Drawer.code, Drawer.state, Drawer.leather_in, Drawer.lining_in,
                   Drawer.received_at, Drawer.sended_at)
            .select_from(Piece)
            .join(SKU, SKU.id == Piece.sku_id)
            .join(Style, Style.id == SKU.style_id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
            .join(Client, Client.id == ClientOrder.client_id)
            .outerjoin(Drawer, Drawer.id == Piece.drawer_id)
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
         style_name, article, order_number, client_name,
         style_id, order_id, style_code, style_needs_lining, piece_needs_lining,
         drawer_code, drawer_state, leather_in, lining_in,
         received_at, sended_at) = row

        # WHO DID THE WORK, per stage — the employee_id as well as the name, so a
        # row can link through to that worker rather than only printing them.
        ev = (await self.db.execute(
            select(Operation.code, Operation.label, Operation.sequence,
                   Employee.name, ProductionEvent.work_date,
                   ProductionEvent.entered_by, ProductionEvent.created_at,
                   Employee.id, Employee.designation,
                   ProductionEvent.consumption_qty)
            .select_from(ProductionEvent)
            .join(Operation, Operation.id == ProductionEvent.operation_id)
            .join(Employee, Employee.id == ProductionEvent.employee_id)
            .where(ProductionEvent.piece_id == pid)
            .order_by(ProductionEvent.work_date, ProductionEvent.created_at)
        )).all()
        stages: list[dict] = []
        seen: set = set()
        for (op_code, op_label, _s, emp, wdate, entered_by, created,
             emp_id, designation, consumption) in ev:
            stages.append({
                "stage_code": op_code,
                "stage_label": op_label,
                "employee_id": str(emp_id) if emp_id else None,
                "employee_name": emp,
                "designation": designation,
                "work_date": wdate.isoformat() if wdate else None,
                "logged_at": created.isoformat() if created else None,
                "entered_by": entered_by,
                "is_rework": op_code in seen,
                "consumption": float(consumption) if consumption is not None else None,
            })
            seen.add(op_code)

        done_codes = {_norm(s["stage_code"]) for s in stages}
        event_stage = stages[-1]["stage_code"] if stages else None

        # THE EFFECTIVE LINING REQUIREMENT, through the one shared resolver — not
        # `piece.needs_lining`, which is a copy taken at mint time. The piece page
        # is where somebody goes to find out why a garment has not moved, so it
        # must agree with the gate that is refusing to move it.
        from app.modules.drawers.service import DrawerService
        piece_obj = await self.db.get(Piece, pid)
        needs_lining, lining_reason = await DrawerService(self.db)._needs_lining(
            piece_obj)

        disp = display_stage(current_event_stage=event_stage,
                             drawer_state=drawer_state,
                             needs_lining=needs_lining)

        # EVERY STAGE, NOT JUST THE LOGGED ONES. A history list alone cannot show
        # what a piece has NOT done, and "what is it waiting on" is the question
        # this page exists to answer. A lining cut on a style declared
        # leather-only is reported as not_applicable rather than pending, so it
        # does not read as permanently outstanding work.
        by_code = {_norm(s["stage_code"]): s for s in stages}
        checklist = []
        for stage_code in _PIPELINE_STAGES:
            entry = {"stage": stage_code,
                     "label": stage_code.replace("_", " ").title()}
            hit = by_code.get(stage_code)
            if hit is not None:
                entry.update(state="completed", employee_name=hit["employee_name"],
                             work_date=hit["work_date"])
            elif (stage_code == ProductionStage.LINING_CUTTING.value
                  and not needs_lining):
                entry.update(state="not_applicable",
                             reason=f"{code} needs no lining.")
            else:
                entry.update(state="pending", employee_name=None, work_date=None)
            checklist.append(entry)

        return {
            "piece_id": str(pid),
            "piece_code": code,
            "bundle_id": code,
            "seq": pseq,
            "serial": f"{pseq:03d}" if pseq is not None else None,
            "sku_code": skucode,
            "sku_label": sku_label(style_name, color_name, color_code, size),
            "colour": color_name or color_code,
            "size": size,
            "style_id": str(style_id),
            "style_code": style_code,
            "style_name": style_name,
            "article": article,
            "order_id": str(order_id),
            "order_number": order_number,
            "client": client_name,
            "current_stage": event_stage,
            "display_stage": disp["display_stage"],
            "display_label": disp["label"],
            "in_store": disp["in_store"],
            "needs_lining": needs_lining,
            "lining_reason": lining_reason,
            "lining_declared_on_style": style_needs_lining,
            "completed": _TERMINAL_STAGE in done_codes,
            # WHERE IT IS SITTING, on the piece page itself — the drawer is the
            # thing nobody can see from anywhere except the store hub.
            "store": {
                "drawer_code": drawer_code,
                "drawer_state": drawer_state,
                "holding": (holding_label(leather_in=leather_in,
                                          lining_in=lining_in)
                            if drawer_code else None),
                "leather_in": bool(leather_in) if drawer_code else None,
                "lining_in": bool(lining_in) if drawer_code else None,
                "received_at": received_at.isoformat() if received_at else None,
                "sended_at": sended_at.isoformat() if sended_at else None,
                "awaiting": [p for p, missing in
                             (("LEATHER", drawer_code and not leather_in),
                              ("LINING", drawer_code and needs_lining
                               and not lining_in))
                             if missing],
            },
            "checklist": checklist,
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