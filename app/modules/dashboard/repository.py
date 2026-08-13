"""
================================================================================
modules/dashboard/repository.py — Manager Dashboard data access (READ-ONLY)
================================================================================
This module owns NO tables and writes NOTHING — it mirrors the analytics module:
a query surface over production_event / piece / sku / style / client_order /
material_lot / drawer / employee / audit_log.

DESIGN CONTRACT (why every method here is a single grouped aggregate)
    No widget fans out into one-query-per-row. Every method below is ONE round
    trip that returns ONLY the columns its widget needs. The service stitches the
    shaped rows together in Python — no lazy relationship loads, no N+1.

STATUS DERIVATION (single source of truth, matched to the app's own model)
    There is no "status" column on Piece. A piece's position is derived exactly as
    analytics/production/store_display already derive it:
      • current stage   = Piece.current_operation_id -> Operation.code
      • COMPLETED (order) = current stage is the terminal chain stage
                            (ProductionStage.leather_chain()[-1] == PACKAGE_EXPORT)
      • completed @ stage = the piece has >=1 event at that stage's op code
      • ASSIGNED @ stage  = the piece has >=1 event at that stage's op code
      • REWORK            = a piece with >1 event at the SAME operation
                            (the app's own definition — ScanResult.rework)
      • consumption       = SUM(ProductionEvent.consumption_qty) at cut stages
                            (dcm² for leather via leather_lot_id; mtrs for lining
                             via lining_lot_id — the lot uom disambiguates)
      • STORE             = a DERIVED state of the piece's DRAWER, never an event
                            (see core/store_display.py). The store metrics below
                            read Drawer.state / leather_in / lining_in, never a
                            "STORE" production event, which does not exist.

    DAMAGE is intentionally absent: the schema has no damage state or table
    (flagged to the CTO). Damage counts are surfaced as 0 by the service with an
    explicit note, never fabricated here.

TENANCY
    Every method accepts client_scope: floor managers are staff (scope=None, read
    across clients); the parameter exists so the same queries serve a scoped
    CLIENT token without a second code path. When set, it filters on
    ClientOrder.client_id, so a cross-tenant id yields empty, never another
    client's rows.
================================================================================
"""
from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import ProductionStage
from app.modules.clients.models import SKU, Client, ClientOrder, Style
from app.modules.employees.models import Employee
from app.modules.production.models import Operation, Piece, ProductionEvent

# ── canonical stage codes (Operation.code equals these uppercase values) ──────
_TERMINAL_STAGE = ProductionStage.leather_chain()[-1].value          # PACKAGE_EXPORT
_LEATHER_CUT = ProductionStage.LEATHER_CUTTING.value
_LINING_CUT = ProductionStage.LINING_CUTTING.value
_FUSING = ProductionStage.FUSING.value
_PASTING = ProductionStage.PASTING.value
_LINE_STITCHING = ProductionStage.LINE_STITCHING.value
_SHELL_STITCHING = ProductionStage.SHELL_STITCHING.value
_FINAL_FINISH = ProductionStage.FINAL_FINISH.value
_FINAL_INSPECTION = ProductionStage.FINAL_INSPECTION.value

_CUT_STAGES = (_LEATHER_CUT, _LINING_CUT)

# Stitching-manager stages, grouped per the requirements doc (§1, §5, §8).
_PRE_STORE_STAGES = (_PASTING, _FUSING)
_POST_STORE_STAGES = (_LINE_STITCHING, _SHELL_STITCHING, _FINAL_FINISH)
_STITCH_STAGES = _PRE_STORE_STAGES + _POST_STORE_STAGES
# Every op the stitching funnel needs a completed-count for (adds the cut entry
# and inspection so "received" and "ready-for-inspection" math has its inputs).
_STITCH_FUNNEL_OPS = (
    _LEATHER_CUT, _FUSING, _PASTING, _LINE_STITCHING,
    _SHELL_STITCHING, _FINAL_FINISH, _FINAL_INSPECTION,
)

# Drawer state values (mirror DrawerState; kept as plain strings exactly like
# core/store_display.py so this read path imports nothing heavy).
_D_WAITING = "waiting"
_D_MERGED = "merged"
_D_HOLDING_LEATHER = "holding_leather"
_D_HOLDING_LINING = "holding_lining"
_D_HOLDING_BOTH = "holding_both"
_D_RECEIVED = "received"
_D_SENDED = "sended"
_D_HOLDING = (_D_HOLDING_LEATHER, _D_HOLDING_LINING, _D_HOLDING_BOTH)


class DashboardRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ── shared predicate helpers ─────────────────────────────────────────────
    @staticmethod
    def _scope(stmt, client_scope: uuid.UUID | None):
        """Append the tenancy predicate when the caller is a scoped CLIENT."""
        if client_scope is not None:
            stmt = stmt.where(ClientOrder.client_id == client_scope)
        return stmt

    # ══════════════════════════════════════════════════════════════ CUTTING
    # (original methods — unchanged behaviour)
    # ═════════════════════════════════════════════════════════════════ KPIs
    async def production_kpis(
        self, *, today: date, client_scope: uuid.UUID | None,
        order_id: uuid.UUID | None = None,
    ) -> dict:
        """Section 3 — production KPIs, in ONE pass over piece + its stage."""
        stage = Operation.code
        term_ev = ProductionEvent

        completed_expr = case((stage == _TERMINAL_STAGE, 1), else_=0)
        completed_today_expr = case(
            (and_(stage == _TERMINAL_STAGE, term_ev.work_date == today), 1),
            else_=0,
        )

        stmt = (
            select(
                func.count(func.distinct(Piece.id)).label("total_pieces"),
                func.coalesce(func.sum(completed_expr), 0).label("completed"),
                func.coalesce(func.sum(completed_today_expr), 0).label("completed_today"),
            )
            .select_from(Piece)
            .join(SKU, SKU.id == Piece.sku_id)
            .join(Style, Style.id == SKU.style_id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
            .outerjoin(Operation, Operation.id == Piece.current_operation_id)
            .outerjoin(
                term_ev,
                and_(term_ev.piece_id == Piece.id,
                     term_ev.operation_id == Piece.current_operation_id),
            )
            .where(Piece.is_active.is_(True))
        )
        if order_id is not None:
            stmt = stmt.where(ClientOrder.id == order_id)
        stmt = self._scope(stmt, client_scope)
        row = (await self.db.execute(stmt)).one()

        ord_stmt = (
            select(func.coalesce(func.sum(SKU.qty_ordered), 0))
            .select_from(SKU)
            .join(Style, Style.id == SKU.style_id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
        )
        if order_id is not None:
            ord_stmt = ord_stmt.where(ClientOrder.id == order_id)
        ord_stmt = self._scope(ord_stmt, client_scope)
        qty_ordered = int((await self.db.execute(ord_stmt)).scalar_one() or 0)

        total_pieces = int(row.total_pieces or 0)
        completed = int(row.completed or 0)
        completed_today = int(row.completed_today or 0)

        assigned = await self._assigned_count(
            today=today, client_scope=client_scope, order_id=order_id,
            stages=_CUT_STAGES)
        rework = await self._rework_count(
            today=today, client_scope=client_scope, order_id=order_id)

        return {
            "total_order_pieces": qty_ordered,
            "minted_pieces": total_pieces,
            "assigned_pieces": assigned["overall"],
            "assigned_today": assigned["today"],
            "completed_today": completed_today,
            "overall_completed": completed,
            "pending_today": max(assigned["today"] - completed_today, 0),
            "overall_pending": max(qty_ordered - completed, 0),
            "rework_pieces": rework["overall"],
            "rework_today": rework["today"],
        }

    async def _assigned_count(
        self, *, today: date, client_scope: uuid.UUID | None,
        order_id: uuid.UUID | None, stages: tuple[str, ...] = _CUT_STAGES,
    ) -> dict:
        """Distinct pieces with an event at any of `stages` (overall + today).

        Generalised: `stages` defaults to the two cut entries (cutting dashboard);
        the lining dashboard passes (LINING_CUTTING,) and the stitching stages
        pass their own op code.
        """
        base = (
            select(
                func.count(func.distinct(ProductionEvent.piece_id)).label("overall"),
                func.count(func.distinct(
                    case((ProductionEvent.work_date == today,
                          ProductionEvent.piece_id))
                )).label("today"),
            )
            .select_from(ProductionEvent)
            .join(Operation, Operation.id == ProductionEvent.operation_id)
            .join(SKU, SKU.id == ProductionEvent.sku_id)
            .join(Style, Style.id == SKU.style_id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
            .where(Operation.code.in_(stages))
        )
        if order_id is not None:
            base = base.where(ClientOrder.id == order_id)
        base = self._scope(base, client_scope)
        r = (await self.db.execute(base)).one()
        return {"overall": int(r.overall or 0), "today": int(r.today or 0)}

    async def _rework_count(
        self, *, today: date, client_scope: uuid.UUID | None,
        order_id: uuid.UUID | None, stages: tuple[str, ...] | None = None,
    ) -> dict:
        """A piece counts as rework once it has >1 event at ANY single operation.

        `stages`, when given, restricts the definition to rework at those op codes
        (used by the lining/stitching stage dashboards).
        """
        dup = (
            select(
                ProductionEvent.piece_id.label("piece_id"),
                func.max(
                    case((ProductionEvent.work_date == today, 1), else_=0)
                ).label("touched_today"),
            )
            .select_from(ProductionEvent)
            .join(Operation, Operation.id == ProductionEvent.operation_id)
            .join(SKU, SKU.id == ProductionEvent.sku_id)
            .join(Style, Style.id == SKU.style_id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
            .where(ProductionEvent.piece_id.isnot(None))
        )
        if stages is not None:
            dup = dup.where(Operation.code.in_(stages))
        if order_id is not None:
            dup = dup.where(ClientOrder.id == order_id)
        dup = self._scope(dup, client_scope)
        dup = dup.group_by(ProductionEvent.piece_id, ProductionEvent.operation_id) \
                 .having(func.count(ProductionEvent.id) > 1) \
                 .subquery()

        r = (await self.db.execute(
            select(
                func.count(func.distinct(dup.c.piece_id)),
                func.count(func.distinct(
                    case((dup.c.touched_today == 1, dup.c.piece_id))
                )),
            )
        )).one()
        return {"overall": int(r[0] or 0), "today": int(r[1] or 0)}

    # ═══════════════════════════════════════════════════ leather / DCM KPIs
    async def leather_kpis(self, *, client_scope: uuid.UUID | None) -> dict:
        """Section 3 leather block."""
        return await self._material_kpis(category="leather", cut_stage=_LEATHER_CUT)

    async def _material_kpis(self, *, category: str, cut_stage: str) -> dict:
        """Available (Σ on_hand of active lots of `category`) + consumed
        (Σ consumption_qty at `cut_stage`). Two scalar reads.

        Generalised so the lining dashboard reuses it with category='lining' and
        cut_stage=LINING_CUTTING. Consumed uses the correct lot column implicitly:
        events at LEATHER_CUTTING carry leather consumption, events at
        LINING_CUTTING carry lining consumption, so filtering by op code isolates
        the right material.
        """
        from app.modules.barcode.models import MaterialLot   # lots live here

        avail = (await self.db.execute(
            select(func.coalesce(func.sum(MaterialLot.on_hand), 0))
            .where(MaterialLot.is_active.is_(True),
                   func.lower(MaterialLot.category) == category)
        )).scalar_one()

        consumed = (await self.db.execute(
            select(func.coalesce(func.sum(ProductionEvent.consumption_qty), 0))
            .select_from(ProductionEvent)
            .join(Operation, Operation.id == ProductionEvent.operation_id)
            .where(Operation.code == cut_stage)
        )).scalar_one()

        return {
            "available": float(avail or 0),
            "consumed": float(consumed or 0),
            "remaining": float(avail or 0),   # on_hand already net of cuts
        }

    # ══════════════════════════════════════════════════════ current order
    async def current_order(
        self, *, client_scope: uuid.UUID | None,
        order_id: uuid.UUID | None = None,
    ) -> dict | None:
        """Section 4 — the running order/style (defaults to most recent in scope).
        ONE grouped query gives the whole per-style stage distribution."""
        head_stmt = (
            select(
                ClientOrder.id, ClientOrder.order_number, ClientOrder.order_date,
                ClientOrder.delivery_deadline, Client.name,
            )
            .select_from(ClientOrder)
            .join(Client, Client.id == ClientOrder.client_id)
        )
        if order_id is not None:
            head_stmt = head_stmt.where(ClientOrder.id == order_id)
        head_stmt = self._scope(head_stmt, client_scope)
        head_stmt = head_stmt.order_by(ClientOrder.created_at.desc()).limit(1)
        head = (await self.db.execute(head_stmt)).first()
        if not head:
            return None
        oid, onum, odate, deadline, client_name = head

        rows = (await self.db.execute(
            select(
                Style.id, Style.name, Style.article, Style.thickness,
                Operation.code, func.count(Piece.id),
            )
            .select_from(Piece)
            .join(SKU, SKU.id == Piece.sku_id)
            .join(Style, Style.id == SKU.style_id)
            .outerjoin(Operation, Operation.id == Piece.current_operation_id)
            .where(Style.client_order_id == oid, Piece.is_active.is_(True))
            .group_by(Style.id, Style.name, Style.article, Style.thickness,
                      Operation.code)
        )).all()

        return {
            "order_id": oid, "order_number": onum, "order_date": odate,
            "delivery_deadline": deadline, "client": client_name,
            "rows": rows,
        }

    # ═══════════════════════════════════════════════════ per-cutter section
    async def cutter_performance(
        self, *, today: date, client_scope: uuid.UUID | None,
        order_id: uuid.UUID | None = None,
    ) -> list:
        """Section 5 — one row per cutter (leather cut). ONE grouped query."""
        return await self._stage_employees(
            stages=(_LEATHER_CUT,), today=today, client_scope=client_scope,
            order_id=order_id, consumption=True)

    # ═══════════════════════════════════════════════════ DCM / leather grid
    async def leather_by_lot(self, *, limit: int = 200) -> list:
        """Section 8 — available-leather + DCM grid. ONE query."""
        return await self._material_by_lot(
            category="leather", lot_col=ProductionEvent.leather_lot_id, limit=limit)

    async def _material_by_lot(
        self, *, category: str, lot_col, limit: int = 200,
    ) -> list:
        """One row per lot of `category` with available (on_hand), consumed-from
        -this-lot and pieces-from-this-lot, via ONE correlated LEFT JOIN aggregate
        on cut events referencing the lot. `lot_col` is leather_lot_id or
        lining_lot_id, so the same query serves both material sides."""
        from app.modules.barcode.models import MaterialLot

        consumed_sq = (
            select(
                lot_col.label("lot_id"),
                func.coalesce(func.sum(ProductionEvent.consumption_qty), 0)
                    .label("consumed"),
                func.count(func.distinct(ProductionEvent.piece_id)).label("pieces"),
            )
            .where(lot_col.isnot(None))
            .group_by(lot_col)
            .subquery()
        )

        stmt = (
            select(
                MaterialLot.id, MaterialLot.subtype, MaterialLot.article,
                MaterialLot.colour, MaterialLot.thickness, MaterialLot.uom,
                MaterialLot.on_hand,
                func.coalesce(consumed_sq.c.consumed, 0),
                func.coalesce(consumed_sq.c.pieces, 0),
            )
            .select_from(MaterialLot)
            .outerjoin(consumed_sq, consumed_sq.c.lot_id == MaterialLot.id)
            .where(MaterialLot.is_active.is_(True),
                   func.lower(MaterialLot.category) == category)
            .order_by(MaterialLot.article, MaterialLot.colour)
            .limit(limit)
        )
        return (await self.db.execute(stmt)).all()

    # ═══════════════════════════════════════════════ per-order progress grid
    async def order_progress(
        self, *, client_scope: uuid.UUID | None,
    ) -> list:
        """Section 7 — every active order/style with ordered vs completed. ONE
        grouped query: completed derived from the terminal current stage."""
        completed_expr = case((Operation.code == _TERMINAL_STAGE, 1), else_=0)
        stmt = (
            select(
                ClientOrder.id, ClientOrder.order_number,
                ClientOrder.order_date, ClientOrder.delivery_deadline,
                Style.id, Style.name, Style.article,
                func.count(func.distinct(Piece.id)).label("minted"),
                func.coalesce(func.sum(completed_expr), 0).label("completed"),
            )
            .select_from(Piece)
            .join(SKU, SKU.id == Piece.sku_id)
            .join(Style, Style.id == SKU.style_id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
            .outerjoin(Operation, Operation.id == Piece.current_operation_id)
            .where(Piece.is_active.is_(True))
        )
        stmt = self._scope(stmt, client_scope)
        stmt = stmt.group_by(
            ClientOrder.id, ClientOrder.order_number, ClientOrder.order_date,
            ClientOrder.delivery_deadline, Style.id, Style.name, Style.article,
        ).order_by(ClientOrder.delivery_deadline.is_(None),
                   ClientOrder.delivery_deadline)

        ord_rows = (await self.db.execute(
            select(Style.id, func.coalesce(func.sum(SKU.qty_ordered), 0))
            .select_from(SKU)
            .join(Style, Style.id == SKU.style_id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
            .where(*([ClientOrder.client_id == client_scope] if client_scope else []))
            .group_by(Style.id)
        )).all()
        ordered_by_style = {sid: int(n or 0) for sid, n in ord_rows}
        return [(*r, ordered_by_style.get(r[4], 0))
                for r in (await self.db.execute(stmt)).all()]

    # ═══════════════════════════════════════════════════ daily production
    async def daily_production(
        self, *, start: date, end: date, client_scope: uuid.UUID | None,
        assigned_stages: tuple[str, ...] = _CUT_STAGES,
        completed_stage: str = _TERMINAL_STAGE,
    ) -> list:
        """Section 6 — per-date assigned / completed / events across the window.
        ONE grouped query keyed on work_date. `assigned_stages`/`completed_stage`
        are parametrised so the lining dashboard reuses it with LINING_CUTTING."""
        assigned_expr = case(
            (Operation.code.in_(assigned_stages), ProductionEvent.piece_id))
        completed_expr = case(
            (Operation.code == completed_stage, ProductionEvent.piece_id))
        stmt = (
            select(
                ProductionEvent.work_date,
                func.count(func.distinct(assigned_expr)).label("assigned"),
                func.count(func.distinct(completed_expr)).label("completed"),
                func.count(ProductionEvent.id).label("events"),
            )
            .select_from(ProductionEvent)
            .join(Operation, Operation.id == ProductionEvent.operation_id)
            .join(SKU, SKU.id == ProductionEvent.sku_id)
            .join(Style, Style.id == SKU.style_id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
            .where(ProductionEvent.work_date >= start,
                   ProductionEvent.work_date <= end)
        )
        stmt = self._scope(stmt, client_scope)
        stmt = stmt.group_by(ProductionEvent.work_date) \
                   .order_by(ProductionEvent.work_date.desc())
        return (await self.db.execute(stmt)).all()

    # ═══════════════════════════════════ per-piece consumption (analysis grid)
    async def piece_consumption(
        self, *, client_scope: uuid.UUID | None,
        order_id: uuid.UUID | None = None,
        employee_id: uuid.UUID | None = None,
        start: date | None = None, end: date | None = None,
        stages: tuple[str, ...] = _CUT_STAGES,
        material_lot_col=None,
        limit: int = 500,
    ) -> list:
        """Sections 11 & 14 — one row per cut EVENT with actual consumption, the
        worker, the lot and the piece. ONE query; no expected/variance because the
        schema stores no expected baseline (that is the BOM, flagged).

        `stages` + `material_lot_col` parametrise leather (default) vs lining."""
        from app.modules.barcode.models import MaterialLot
        lot_col = material_lot_col if material_lot_col is not None \
            else ProductionEvent.leather_lot_id

        stmt = (
            select(
                Piece.code, ProductionEvent.work_date,
                Employee.name, Operation.code,
                ProductionEvent.consumption_qty,
                MaterialLot.article, MaterialLot.colour, MaterialLot.thickness,
                Style.name, ClientOrder.order_number, SKU.size,
            )
            .select_from(ProductionEvent)
            .join(Operation, Operation.id == ProductionEvent.operation_id)
            .join(Employee, Employee.id == ProductionEvent.employee_id)
            .join(Piece, Piece.id == ProductionEvent.piece_id)
            .join(SKU, SKU.id == ProductionEvent.sku_id)
            .join(Style, Style.id == SKU.style_id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
            .outerjoin(MaterialLot, MaterialLot.id == lot_col)
            .where(Operation.code.in_(stages),
                   ProductionEvent.consumption_qty.isnot(None))
        )
        if order_id is not None:
            stmt = stmt.where(ClientOrder.id == order_id)
        if employee_id is not None:
            stmt = stmt.where(ProductionEvent.employee_id == employee_id)
        if start is not None:
            stmt = stmt.where(ProductionEvent.work_date >= start)
        if end is not None:
            stmt = stmt.where(ProductionEvent.work_date <= end)
        stmt = self._scope(stmt, client_scope)
        stmt = stmt.order_by(ProductionEvent.work_date.desc()).limit(limit)
        return (await self.db.execute(stmt)).all()

    # ═══════════════════════════════════════════ employee drill-down (pieces)
    async def pieces_for_employee(
        self, *, employee_id: uuid.UUID, client_scope: uuid.UUID | None,
        limit: int = 500,
    ) -> list:
        """Section 5 drill-down — the pieces a worker touched, with current stage.
        ONE query; distinct pieces via the events this employee logged."""
        stmt = (
            select(
                Piece.code, Piece.seq, SKU.size,
                SKU.color_name, SKU.color_code, Style.name,
                Operation.code, func.max(ProductionEvent.work_date),
            )
            .select_from(ProductionEvent)
            .join(Piece, Piece.id == ProductionEvent.piece_id)
            .join(SKU, SKU.id == Piece.sku_id)
            .join(Style, Style.id == SKU.style_id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
            .outerjoin(Operation, Operation.id == Piece.current_operation_id)
            .where(ProductionEvent.employee_id == employee_id,
                   ProductionEvent.piece_id.isnot(None))
        )
        stmt = self._scope(stmt, client_scope)
        stmt = stmt.group_by(
            Piece.code, Piece.seq, SKU.size, SKU.color_name, SKU.color_code,
            Style.name, Operation.code,
        ).order_by(func.max(ProductionEvent.work_date).desc()).limit(limit)
        return (await self.db.execute(stmt)).all()

    # ══════════════════════════════════════════════════════════════ SHARED
    # Stage-generic building blocks used by Lining + Stitching.
    # ═══════════════════════════════════════════════════════════════════════
    async def _stage_employees(
        self, *, stages: tuple[str, ...], today: date,
        client_scope: uuid.UUID | None, order_id: uuid.UUID | None = None,
        consumption: bool = False, group_by_stage: bool = False,
    ) -> list:
        """ONE grouped query — per employee (optionally per stage) throughput at
        `stages`: distinct pieces, distinct-today, events-today, and (optionally)
        summed consumption. Powers the cutter grid, the lining employee grid and
        the stitching per-stage employee grid, all N+1-free.

        Returns tuples:
          (emp_id, name, designation, [op_code if group_by_stage],
           assigned, assigned_today, events_today, consumed)
        """
        cols = [Employee.id, Employee.name, Employee.designation]
        grp = [Employee.id, Employee.name, Employee.designation]
        if group_by_stage:
            cols.append(Operation.code)
            grp.append(Operation.code)
        cols += [
            func.count(func.distinct(ProductionEvent.piece_id)).label("assigned"),
            func.count(func.distinct(
                case((ProductionEvent.work_date == today,
                      ProductionEvent.piece_id))
            )).label("assigned_today"),
            func.count(
                case((ProductionEvent.work_date == today, ProductionEvent.id))
            ).label("events_today"),
            func.coalesce(func.sum(ProductionEvent.consumption_qty), 0).label("consumed")
            if consumption else func.count(ProductionEvent.id).label("events"),
        ]
        stmt = (
            select(*cols)
            .select_from(ProductionEvent)
            .join(Employee, Employee.id == ProductionEvent.employee_id)
            .join(Operation, Operation.id == ProductionEvent.operation_id)
            .join(SKU, SKU.id == ProductionEvent.sku_id)
            .join(Style, Style.id == SKU.style_id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
            .where(Operation.code.in_(stages))
        )
        if order_id is not None:
            stmt = stmt.where(ClientOrder.id == order_id)
        stmt = self._scope(stmt, client_scope)
        stmt = stmt.group_by(*grp).order_by(
            func.count(func.distinct(ProductionEvent.piece_id)).desc())
        return (await self.db.execute(stmt)).all()

    async def stage_completed_today(
        self, *, stage: str, today: date, client_scope: uuid.UUID | None,
        order_id: uuid.UUID | None = None,
    ) -> int:
        """Distinct pieces with an event at `stage` today. ONE scalar read."""
        stmt = (
            select(func.count(func.distinct(ProductionEvent.piece_id)))
            .select_from(ProductionEvent)
            .join(Operation, Operation.id == ProductionEvent.operation_id)
            .join(SKU, SKU.id == ProductionEvent.sku_id)
            .join(Style, Style.id == SKU.style_id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
            .where(Operation.code == stage, ProductionEvent.work_date == today)
        )
        if order_id is not None:
            stmt = stmt.where(ClientOrder.id == order_id)
        stmt = self._scope(stmt, client_scope)
        return int((await self.db.execute(stmt)).scalar_one() or 0)

    # ══════════════════════════════════════════════════════════════ LINING
    async def lining_kpis(self, *, client_scope: uuid.UUID | None) -> dict:
        return await self._material_kpis(category="lining", cut_stage=_LINING_CUT)

    async def lining_required_pieces(
        self, *, client_scope: uuid.UUID | None, order_id: uuid.UUID | None = None,
    ) -> int:
        """Active pieces whose needs_lining is True — the population the lining
        stage must process. ONE scalar read."""
        stmt = (
            select(func.count(func.distinct(Piece.id)))
            .select_from(Piece)
            .join(SKU, SKU.id == Piece.sku_id)
            .join(Style, Style.id == SKU.style_id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
            .where(Piece.is_active.is_(True), Piece.needs_lining.is_(True))
        )
        if order_id is not None:
            stmt = stmt.where(ClientOrder.id == order_id)
        stmt = self._scope(stmt, client_scope)
        return int((await self.db.execute(stmt)).scalar_one() or 0)

    async def lining_production_kpis(
        self, *, today: date, client_scope: uuid.UUID | None,
        order_id: uuid.UUID | None = None,
    ) -> dict:
        """Lining §3 KPIs. assigned/completed derive from LINING_CUTTING events;
        pending is against the lining-required population."""
        ord_stmt = (
            select(func.coalesce(func.sum(SKU.qty_ordered), 0))
            .select_from(SKU)
            .join(Style, Style.id == SKU.style_id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
        )
        if order_id is not None:
            ord_stmt = ord_stmt.where(ClientOrder.id == order_id)
        ord_stmt = self._scope(ord_stmt, client_scope)
        qty_ordered = int((await self.db.execute(ord_stmt)).scalar_one() or 0)

        required = await self.lining_required_pieces(
            client_scope=client_scope, order_id=order_id)
        assigned = await self._assigned_count(
            today=today, client_scope=client_scope, order_id=order_id,
            stages=(_LINING_CUT,))
        completed_today = await self.stage_completed_today(
            stage=_LINING_CUT, today=today, client_scope=client_scope,
            order_id=order_id)
        rework = await self._rework_count(
            today=today, client_scope=client_scope, order_id=order_id,
            stages=(_LINING_CUT,))

        overall_completed = assigned["overall"]     # lining is one cut event
        return {
            "total_order_pieces": qty_ordered,
            "lining_required_pieces": required,
            "assigned_pieces": assigned["overall"],
            "assigned_today": assigned["today"],
            "completed_today": completed_today,
            "overall_completed": overall_completed,
            "pending_today": max(required - overall_completed, 0),
            "overall_pending": max(required - overall_completed, 0),
            "rework_pieces": rework["overall"],
            "rework_today": rework["today"],
        }

    async def lining_employees(
        self, *, today: date, client_scope: uuid.UUID | None,
        order_id: uuid.UUID | None = None,
    ) -> list:
        return await self._stage_employees(
            stages=(_LINING_CUT,), today=today, client_scope=client_scope,
            order_id=order_id, consumption=True)

    async def lining_by_lot(self, *, limit: int = 200) -> list:
        return await self._material_by_lot(
            category="lining", lot_col=ProductionEvent.lining_lot_id, limit=limit)

    async def lining_upcoming(
        self, *, client_scope: uuid.UUID | None,
        order_id: uuid.UUID | None = None, limit: int = 300,
    ) -> list:
        """Lining §14 — pieces that have a leather cut but no lining cut yet, i.e.
        lining work that is upcoming. ONE grouped query; grouped to a compact
        style/colour/size row so the grid is not per-piece.

        LEFT JOIN to the lining-cut event; keep only rows where none exists.
        """
        # pieces with a leather-cut event (subquery: distinct piece ids)
        leather_pieces = (
            select(ProductionEvent.piece_id)
            .select_from(ProductionEvent)
            .join(Operation, Operation.id == ProductionEvent.operation_id)
            .where(Operation.code == _LEATHER_CUT,
                   ProductionEvent.piece_id.isnot(None))
            .distinct()
            .subquery()
        )
        lined_pieces = (
            select(ProductionEvent.piece_id)
            .select_from(ProductionEvent)
            .join(Operation, Operation.id == ProductionEvent.operation_id)
            .where(Operation.code == _LINING_CUT,
                   ProductionEvent.piece_id.isnot(None))
            .distinct()
            .subquery()
        )

        stmt = (
            select(
                ClientOrder.id, ClientOrder.order_number,
                ClientOrder.delivery_deadline,
                Style.id, Style.name, Style.article, Style.thickness,
                SKU.color_name, SKU.size,
                func.count(func.distinct(Piece.id)).label("expected_qty"),
            )
            .select_from(Piece)
            .join(SKU, SKU.id == Piece.sku_id)
            .join(Style, Style.id == SKU.style_id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
            .join(leather_pieces, leather_pieces.c.piece_id == Piece.id)
            .outerjoin(lined_pieces, lined_pieces.c.piece_id == Piece.id)
            .where(Piece.is_active.is_(True),
                   Piece.needs_lining.is_(True),
                   lined_pieces.c.piece_id.is_(None))
        )
        if order_id is not None:
            stmt = stmt.where(ClientOrder.id == order_id)
        stmt = self._scope(stmt, client_scope)
        stmt = stmt.group_by(
            ClientOrder.id, ClientOrder.order_number, ClientOrder.delivery_deadline,
            Style.id, Style.name, Style.article, Style.thickness,
            SKU.color_name, SKU.size,
        ).order_by(ClientOrder.delivery_deadline.is_(None),
                   ClientOrder.delivery_deadline).limit(limit)
        return (await self.db.execute(stmt)).all()

    # ══════════════════════════════════════════════════════════════ STITCHING
    async def stitching_funnel(
        self, *, today: date, client_scope: uuid.UUID | None,
        order_id: uuid.UUID | None = None,
    ) -> dict[str, dict]:
        """The whole stitching funnel in ONE grouped query: per op code, the
        distinct pieces with an event there (overall) and today. Returns a dict
        keyed by op code → {'overall': n, 'today': n}."""
        stmt = (
            select(
                Operation.code,
                func.count(func.distinct(ProductionEvent.piece_id)).label("overall"),
                func.count(func.distinct(
                    case((ProductionEvent.work_date == today,
                          ProductionEvent.piece_id))
                )).label("today"),
            )
            .select_from(ProductionEvent)
            .join(Operation, Operation.id == ProductionEvent.operation_id)
            .join(SKU, SKU.id == ProductionEvent.sku_id)
            .join(Style, Style.id == SKU.style_id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
            .where(Operation.code.in_(_STITCH_FUNNEL_OPS))
        )
        if order_id is not None:
            stmt = stmt.where(ClientOrder.id == order_id)
        stmt = self._scope(stmt, client_scope)
        stmt = stmt.group_by(Operation.code)
        rows = (await self.db.execute(stmt)).all()
        return {code: {"overall": int(o or 0), "today": int(t or 0)}
                for code, o, t in rows}

    async def stitching_rework(
        self, *, today: date, client_scope: uuid.UUID | None,
        order_id: uuid.UUID | None = None,
    ) -> dict[str, int]:
        """Rework pieces per stitching op (>1 event at that op). ONE grouped
        query returning {op_code: rework_piece_count}."""
        dup = (
            select(
                Operation.code.label("code"),
                ProductionEvent.piece_id.label("piece_id"),
            )
            .select_from(ProductionEvent)
            .join(Operation, Operation.id == ProductionEvent.operation_id)
            .join(SKU, SKU.id == ProductionEvent.sku_id)
            .join(Style, Style.id == SKU.style_id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
            .where(Operation.code.in_(_STITCH_STAGES),
                   ProductionEvent.piece_id.isnot(None))
        )
        if order_id is not None:
            dup = dup.where(ClientOrder.id == order_id)
        dup = self._scope(dup, client_scope)
        dup = dup.group_by(Operation.code, ProductionEvent.piece_id,
                           ProductionEvent.operation_id) \
                 .having(func.count(ProductionEvent.id) > 1).subquery()
        rows = (await self.db.execute(
            select(dup.c.code, func.count(func.distinct(dup.c.piece_id)))
            .group_by(dup.c.code)
        )).all()
        return {code: int(n or 0) for code, n in rows}

    async def stitching_employees(
        self, *, today: date, client_scope: uuid.UUID | None,
        order_id: uuid.UUID | None = None,
    ) -> list:
        """Per-employee, per-stage stitching throughput. ONE grouped query."""
        return await self._stage_employees(
            stages=_STITCH_STAGES, today=today, client_scope=client_scope,
            order_id=order_id, consumption=False, group_by_stage=True)

    async def stitching_daily(
        self, *, start: date, end: date, client_scope: uuid.UUID | None,
    ) -> list:
        """Per date, per stitching stage: distinct pieces completed + events.
        ONE grouped query keyed on (work_date, op)."""
        stmt = (
            select(
                ProductionEvent.work_date, Operation.code,
                func.count(func.distinct(ProductionEvent.piece_id)).label("completed"),
                func.count(ProductionEvent.id).label("events"),
            )
            .select_from(ProductionEvent)
            .join(Operation, Operation.id == ProductionEvent.operation_id)
            .join(SKU, SKU.id == ProductionEvent.sku_id)
            .join(Style, Style.id == SKU.style_id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
            .where(ProductionEvent.work_date >= start,
                   ProductionEvent.work_date <= end,
                   Operation.code.in_(_STITCH_STAGES))
        )
        stmt = self._scope(stmt, client_scope)
        stmt = stmt.group_by(ProductionEvent.work_date, Operation.code) \
                   .order_by(ProductionEvent.work_date.desc())
        return (await self.db.execute(stmt)).all()

    async def stitching_current_style_funnel(
        self, *, client_scope: uuid.UUID | None, order_id: uuid.UUID | None = None,
    ) -> dict | None:
        """The current style's per-stage piece counts (§15). Picks the most recent
        in-scope order's styles; ONE grouped query over its pieces' events."""
        head = await self.current_order(client_scope=client_scope, order_id=order_id)
        if head is None:
            return None
        oid = head["order_id"]

        rows = (await self.db.execute(
            select(
                Style.id, Style.name, Style.article, Operation.code,
                func.count(func.distinct(ProductionEvent.piece_id)),
            )
            .select_from(ProductionEvent)
            .join(Operation, Operation.id == ProductionEvent.operation_id)
            .join(SKU, SKU.id == ProductionEvent.sku_id)
            .join(Style, Style.id == SKU.style_id)
            .where(Style.client_order_id == oid,
                   Operation.code.in_(_STITCH_FUNNEL_OPS))
            .group_by(Style.id, Style.name, Style.article, Operation.code)
        )).all()

        total_rows = (await self.db.execute(
            select(Style.id, func.count(func.distinct(Piece.id)))
            .select_from(Piece)
            .join(SKU, SKU.id == Piece.sku_id)
            .join(Style, Style.id == SKU.style_id)
            .where(Style.client_order_id == oid, Piece.is_active.is_(True))
            .group_by(Style.id)
        )).all()
        return {
            "order_id": oid, "order_number": head["order_number"],
            "rows": rows,
            "totals": {sid: int(n or 0) for sid, n in total_rows},
        }

    # ══════════════════════════════════════════════════════════════ STORE
    async def store_kpis(self, *, client_scope: uuid.UUID | None) -> dict:
        """§3 store KPIs — drawer counts by state + contents in ONE grouped read.

        Contents are derived from leather_in / lining_in (which survive the
        RECEIVED/SENDED transitions), never from `state`, exactly as
        core/store_display.holding_label does."""
        from app.modules.barcode.models import Drawer

        both = and_(Drawer.leather_in.is_(True), Drawer.lining_in.is_(True))
        leather_only = and_(Drawer.leather_in.is_(True), Drawer.lining_in.is_(False))
        lining_only = and_(Drawer.leather_in.is_(False), Drawer.lining_in.is_(True))
        empty = and_(Drawer.leather_in.is_(False), Drawer.lining_in.is_(False))
        holds_something = Drawer.state.in_(_D_HOLDING)

        stmt = select(
            func.count(Drawer.id).label("total"),
            func.coalesce(func.sum(
                case((and_(holds_something, Drawer.state != _D_SENDED), 1), else_=0)
            ), 0).label("in_store"),
            func.coalesce(func.sum(case((Drawer.state == _D_SENDED, 1), else_=0)), 0)
                .label("sent"),
            func.coalesce(func.sum(case((empty, 1), else_=0)), 0).label("empty"),
            func.coalesce(func.sum(case((Drawer.state == _D_RECEIVED, 1), else_=0)), 0)
                .label("held"),
            func.coalesce(func.sum(case((leather_only, 1), else_=0)), 0).label("leather"),
            func.coalesce(func.sum(case((lining_only, 1), else_=0)), 0).label("lining"),
            func.coalesce(func.sum(case((both, 1), else_=0)), 0).label("both"),
        )
        if client_scope is not None:
            stmt = (
                stmt.select_from(Drawer)
                .join(Piece, Piece.id == Drawer.current_piece_id)
                .join(SKU, SKU.id == Piece.sku_id)
                .join(Style, Style.id == SKU.style_id)
                .join(ClientOrder, ClientOrder.id == Style.client_order_id)
                .where(ClientOrder.client_id == client_scope)
            )
        else:
            stmt = stmt.select_from(Drawer)
        r = (await self.db.execute(stmt)).one()
        return {
            "total_drawers": int(r.total or 0),
            "drawers_in_store": int(r.in_store or 0),
            "drawers_sent": int(r.sent or 0),
            "empty_drawers": int(r.empty or 0),
            "held_drawers": int(r.held or 0),
            "leather_drawers": int(r.leather or 0),
            "lining_drawers": int(r.lining or 0),
            "leather_lining_drawers": int(r.both or 0),
        }

    async def store_handoff(self, *, client_scope: uuid.UUID | None) -> dict:
        """Stitching §7 / store handoff — drawer-state rollups the stitching
        manager reads. ONE grouped query."""
        from app.modules.barcode.models import Drawer

        holding = Drawer.state.in_(_D_HOLDING)
        stmt = select(
            func.coalesce(func.sum(case((holding, 1), else_=0)), 0).label("holding"),
            func.coalesce(func.sum(case((Drawer.state == _D_RECEIVED, 1), else_=0)), 0)
                .label("received"),
            func.coalesce(func.sum(case((Drawer.state == _D_SENDED, 1), else_=0)), 0)
                .label("sended"),
            func.coalesce(func.sum(
                case((and_(Drawer.current_piece_id.isnot(None),
                           Drawer.state.in_((*_D_HOLDING, _D_RECEIVED))), 1), else_=0)
            ), 0).label("in_drawer"),
        )
        if client_scope is not None:
            stmt = (
                stmt.select_from(Drawer)
                .join(Piece, Piece.id == Drawer.current_piece_id)
                .join(SKU, SKU.id == Piece.sku_id)
                .join(Style, Style.id == SKU.style_id)
                .join(ClientOrder, ClientOrder.id == Style.client_order_id)
                .where(ClientOrder.client_id == client_scope)
            )
        else:
            stmt = stmt.select_from(Drawer)
        r = (await self.db.execute(stmt)).one()
        return {
            "holding": int(r.holding or 0),
            "received": int(r.received or 0),
            "sended": int(r.sended or 0),
            "in_drawer": int(r.in_drawer or 0),
        }

    async def drawer_grid(
        self, *, client_scope: uuid.UUID | None,
        style_id: uuid.UUID | None = None,
        state: str | None = None,
        material_type: str | None = None,   # LEATHER | LINING | BOTH
        limit: int = 500,
    ) -> list:
        """§4/§6 drawer list joined to its current piece → style → order. ONE
        query. LEFT joins so empty/unmerged drawers still appear (they have no
        piece). Filterable by style, state, material type."""
        from app.modules.barcode.models import Drawer

        stmt = (
            select(
                Drawer.id, Drawer.code, Drawer.seq, Drawer.state,
                Drawer.leather_in, Drawer.lining_in,
                Drawer.received_at, Drawer.sended_at, Drawer.created_at,
                Piece.id, Piece.code,
                Style.id, Style.name,
                ClientOrder.id, ClientOrder.order_number,
                ClientOrder.delivery_deadline,
                SKU.color_name, SKU.size,
            )
            .select_from(Drawer)
            .outerjoin(Piece, Piece.id == Drawer.current_piece_id)
            .outerjoin(SKU, SKU.id == Piece.sku_id)
            .outerjoin(Style, Style.id == SKU.style_id)
            .outerjoin(ClientOrder, ClientOrder.id == Style.client_order_id)
        )
        conds = []
        if client_scope is not None:
            conds.append(ClientOrder.client_id == client_scope)
        if style_id is not None:
            conds.append(Style.id == style_id)
        if state is not None:
            conds.append(Drawer.state == state)
        if material_type == "LEATHER":
            conds.append(Drawer.leather_in.is_(True))
        elif material_type == "LINING":
            conds.append(Drawer.lining_in.is_(True))
        elif material_type == "BOTH":
            conds.append(and_(Drawer.leather_in.is_(True), Drawer.lining_in.is_(True)))
        if conds:
            stmt = stmt.where(*conds)
        stmt = stmt.order_by(Drawer.seq).limit(limit)
        return (await self.db.execute(stmt)).all()

    async def store_current_styles(
        self, *, client_scope: uuid.UUID | None, limit: int = 100,
    ) -> list:
        """§5 — styles that currently have material inside the store (drawer in a
        holding/received state). ONE grouped query."""
        from app.modules.barcode.models import Drawer

        in_store = Drawer.state.in_((*_D_HOLDING, _D_RECEIVED))
        both = and_(Drawer.leather_in.is_(True), Drawer.lining_in.is_(True))
        leather_only = and_(Drawer.leather_in.is_(True), Drawer.lining_in.is_(False))
        lining_only = and_(Drawer.leather_in.is_(False), Drawer.lining_in.is_(True))
        ready = Drawer.state == _D_RECEIVED

        stmt = (
            select(
                Style.id, Style.name,
                ClientOrder.id, ClientOrder.order_number,
                ClientOrder.delivery_deadline,
                func.count(func.distinct(Drawer.id)).label("drawers"),
                func.coalesce(func.sum(case((leather_only, 1), else_=0)), 0).label("leather"),
                func.coalesce(func.sum(case((lining_only, 1), else_=0)), 0).label("lining"),
                func.coalesce(func.sum(case((both, 1), else_=0)), 0).label("both"),
                func.coalesce(func.sum(case((ready, 1), else_=0)), 0).label("ready"),
            )
            .select_from(Drawer)
            .join(Piece, Piece.id == Drawer.current_piece_id)
            .join(SKU, SKU.id == Piece.sku_id)
            .join(Style, Style.id == SKU.style_id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
            .where(in_store)
        )
        if client_scope is not None:
            stmt = stmt.where(ClientOrder.client_id == client_scope)
        stmt = stmt.group_by(
            Style.id, Style.name, ClientOrder.id, ClientOrder.order_number,
            ClientOrder.delivery_deadline,
        ).order_by(func.count(func.distinct(Drawer.id)).desc()).limit(limit)
        return (await self.db.execute(stmt)).all()

    async def drawer_detail(self, *, drawer_id: uuid.UUID) -> dict | None:
        """One drawer + its current piece/style/order. ONE query for the head."""
        from app.modules.barcode.models import Drawer

        row = (await self.db.execute(
            select(
                Drawer.id, Drawer.code, Drawer.seq, Drawer.state,
                Drawer.leather_in, Drawer.lining_in,
                Drawer.received_at, Drawer.sended_at, Drawer.created_at,
                Piece.id, Piece.code,
                Style.name, ClientOrder.order_number,
                SKU.color_name, SKU.size,
            )
            .select_from(Drawer)
            .outerjoin(Piece, Piece.id == Drawer.current_piece_id)
            .outerjoin(SKU, SKU.id == Piece.sku_id)
            .outerjoin(Style, Style.id == SKU.style_id)
            .outerjoin(ClientOrder, ClientOrder.id == Style.client_order_id)
            .where(Drawer.id == drawer_id)
        )).first()
        if row is None:
            return None
        keys = ["drawer_id", "code", "seq", "state", "leather_in", "lining_in",
                "received_at", "sended_at", "created_at", "piece_id", "piece_code",
                "style", "order_number", "colour", "size"]
        return dict(zip(keys, row))

    async def drawer_cutters(self, *, piece_id: uuid.UUID) -> list:
        """Who cut the leather / lining held in a drawer — the cut events for its
        current piece. ONE grouped query."""
        stmt = (
            select(
                Operation.code, Employee.id, Employee.name,
                func.max(ProductionEvent.work_date),
            )
            .select_from(ProductionEvent)
            .join(Operation, Operation.id == ProductionEvent.operation_id)
            .join(Employee, Employee.id == ProductionEvent.employee_id)
            .where(ProductionEvent.piece_id == piece_id,
                   Operation.code.in_(_CUT_STAGES))
            .group_by(Operation.code, Employee.id, Employee.name)
        )
        return (await self.db.execute(stmt)).all()

    async def drawer_movement(self, *, drawer_id: uuid.UUID, limit: int = 100) -> list:
        """§17 — a drawer's movement history from the audit trail. ONE query over
        audit_log rows whose entity_id is this drawer (DRAWER_RECEIVED /
        DRAWER_SENDED / MATERIAL_RECEIVED …), newest first."""
        from app.core.models import AuditLog

        stmt = (
            select(AuditLog.action, AuditLog.at, AuditLog.actor_user_id,
                   AuditLog.created_at)
            .where(AuditLog.entity_id == drawer_id)
            .order_by(func.coalesce(AuditLog.at, AuditLog.created_at).desc())
            .limit(limit)
        )
        return (await self.db.execute(stmt)).all()

    async def material_cutter_trace(
        self, *, client_scope: uuid.UUID | None,
        piece_code: str | None = None, style_id: uuid.UUID | None = None,
        material_type: str | None = None,   # LEATHER | LINING
        limit: int = 300,
    ) -> list:
        """§8 employee traceability — for each cut event, who cut the leather /
        lining, for which piece/style/order, and which drawer holds it. ONE query."""
        from app.modules.barcode.models import Drawer

        stages = _CUT_STAGES
        if material_type == "LEATHER":
            stages = (_LEATHER_CUT,)
        elif material_type == "LINING":
            stages = (_LINING_CUT,)

        stmt = (
            select(
                Piece.code, Operation.code, Employee.id, Employee.name,
                Style.name, ClientOrder.order_number,
                SKU.color_name, SKU.size,
                ProductionEvent.work_date, Drawer.code,
            )
            .select_from(ProductionEvent)
            .join(Operation, Operation.id == ProductionEvent.operation_id)
            .join(Employee, Employee.id == ProductionEvent.employee_id)
            .join(Piece, Piece.id == ProductionEvent.piece_id)
            .join(SKU, SKU.id == Piece.sku_id)
            .join(Style, Style.id == SKU.style_id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
            .outerjoin(Drawer, Drawer.id == Piece.drawer_id)
            .where(Operation.code.in_(stages))
        )
        if piece_code is not None:
            stmt = stmt.where(Piece.code == piece_code)
        if style_id is not None:
            stmt = stmt.where(Style.id == style_id)
        stmt = self._scope(stmt, client_scope)
        stmt = stmt.order_by(ProductionEvent.work_date.desc()).limit(limit)
        return (await self.db.execute(stmt)).all()

    # ══════════════════════════════════ piece stage history (stitching trace)
    async def piece_stage_history(self, *, piece_code: str) -> dict | None:
        """§21 traceability — every real production event of a piece, in pipeline
        order, plus the piece's current drawer state for the STORE overlay. Two
        queries: the piece head (+ drawer state) and its events."""
        from app.modules.barcode.models import Drawer

        head = (await self.db.execute(
            select(
                Piece.id, Piece.code, Piece.needs_lining,
                Operation.code, Style.name, ClientOrder.order_number,
                SKU.color_name, SKU.size, Drawer.state,
            )
            .select_from(Piece)
            .join(SKU, SKU.id == Piece.sku_id)
            .join(Style, Style.id == SKU.style_id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
            .outerjoin(Operation, Operation.id == Piece.current_operation_id)
            .outerjoin(Drawer, Drawer.id == Piece.drawer_id)
            .where(Piece.code == piece_code)
        )).first()
        if head is None:
            return None
        (pid, pcode, needs_lining, cur_stage, style, onum,
         colour, size, drawer_state) = head

        events = (await self.db.execute(
            select(Operation.code, Employee.name, ProductionEvent.work_date)
            .select_from(ProductionEvent)
            .join(Operation, Operation.id == ProductionEvent.operation_id)
            .join(Employee, Employee.id == ProductionEvent.employee_id)
            .where(ProductionEvent.piece_id == pid)
            .order_by(ProductionEvent.work_date)
        )).all()

        return {
            "piece_code": pcode, "needs_lining": bool(needs_lining),
            "current_stage": cur_stage, "style": style, "order_number": onum,
            "colour": colour, "size": size, "drawer_state": drawer_state,
            "events": events,
        }
