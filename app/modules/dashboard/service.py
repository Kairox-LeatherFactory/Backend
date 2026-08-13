"""
================================================================================
modules/dashboard/service.py — Manager Dashboard business logic (READ-ONLY)
================================================================================
The service NEVER writes and holds no transaction — it calls the repository's
grouped aggregates and shapes them into the response DTOs, adding only cheap
Python-side math (completion %, delay status, pending, store labels).

QUERY BUDGETS (deliberately bounded — see per-method docstrings)
    cutting.overview     ≈ 11 grouped round trips
    lining.overview      ≈ 12 grouped round trips
    stitching.overview   ≈ 10 grouped round trips
    store.overview       ≈  4 grouped round trips
    None of them is per-piece or per-row.

The `unsupported` map is the honest contract: it names each requirement the
current schema can't yet satisfy and why, so the frontend renders "—" with a
tooltip instead of a fabricated number.
================================================================================
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import ProductionStage
from app.core.store_display import STORE, display_stage, holding_label
from app.modules.dashboard.repository import DashboardRepository
from app.modules.dashboard.schemas import (
    CurrentOrder, CurrentStyle, CutterRow, CuttingDashboard, DailyRow,
    DashboardMeta, DrawerCutterRow, DrawerDetail, DrawerMovementRow, DrawerRow,
    EmployeePieceRow, EmptyDrawerRow, LeatherKPIs, LeatherLotRow,
    LiningDashboard, LiningEmployeeRow, LiningLotRow, LiningMaterialKPIs,
    LiningProductionKPIs, MaterialCutterTrace, OrderProgressRow,
    PieceConsumptionRow, PieceStageHistoryRow, PieceTrace, ProductionKPIs,
    StageBlock, StageDailyRow, StitchingCurrentStyle, StitchingDashboard,
    StitchingEmployeeRow, StitchingKPIs, StitchingStyleStage, StoreCurrentStyleRow,
    StoreDashboard, StoreHandoff, StoreKPIs, UpcomingPieceRow,
)

_TERMINAL = ProductionStage.leather_chain()[-1].value
_LINING_CUT = ProductionStage.LINING_CUTTING.value
_LEATHER_CUT = ProductionStage.LEATHER_CUTTING.value

# Stage presentation metadata (label + pre/post-store section) for stitching.
_STAGE_META: dict[str, tuple[str, str]] = {
    ProductionStage.PASTING.value:         ("Pasting", "PRE_STORE"),
    ProductionStage.FUSING.value:          ("Fusing", "PRE_STORE"),
    ProductionStage.LINE_STITCHING.value:  ("Line Stitching", "POST_STORE"),
    ProductionStage.SHELL_STITCHING.value: ("Shell Stitching", "POST_STORE"),
    ProductionStage.FINAL_FINISH.value:    ("Final Finish", "POST_STORE"),
}
# Order stage blocks are presented in (pre-store, then post-store) pipeline order.
_STAGE_ORDER = [
    ProductionStage.PASTING.value, ProductionStage.FUSING.value,
    ProductionStage.LINE_STITCHING.value, ProductionStage.SHELL_STITCHING.value,
    ProductionStage.FINAL_FINISH.value,
]
_FUSING = ProductionStage.FUSING.value
_PASTING = ProductionStage.PASTING.value
_LINE = ProductionStage.LINE_STITCHING.value
_SHELL = ProductionStage.SHELL_STITCHING.value
_FINAL = ProductionStage.FINAL_FINISH.value
_INSPECTION = ProductionStage.FINAL_INSPECTION.value
_FUNNEL_LABELS = {
    _LEATHER_CUT: "Leather Cutting", _FUSING: "Fusing", _PASTING: "Pasting",
    _LINE: "Line Stitching", _SHELL: "Shell Stitching", _FINAL: "Final Finish",
    _INSPECTION: "Final Inspection",
}
_FUNNEL_PIPELINE = [_LEATHER_CUT, _FUSING, _PASTING, _LINE, _SHELL, _FINAL, _INSPECTION]

# ── honest-contract notes, per dashboard ─────────────────────────────────────
_UNSUPPORTED_DAMAGE = (
    "No damage state or PieceDamage table exists in the schema. Needs a new model "
    "+ migration before damage counts, reasons, corrective-action or DCM-based "
    "damage drill-down can be served. Returned as 0."
)
_UNSUPPORTED_EXPECTED = (
    "Only actual consumption_qty is stored on the cut event. Expected per-piece "
    "consumption / waste is the BOM baseline (Aug-20 scope); until it exists, "
    "expected / variance / waste are null."
)
_UNSUPPORTED_ALLOC = (
    "Allocation/reservation lives in the material_reservation ledger, not on the "
    "cut event. allocated_* / total_*_waste are null here and should be sourced "
    "from the materials module when wired in."
)
_UNSUPPORTED_TARGET_PHOTO = (
    "Employee has no daily_target or photo column. daily_target / achievement_pct "
    "are null; photo is null. Needs employee-profile + target columns."
)

_CUTTING_UNSUPPORTED = {
    "damage_tracking": _UNSUPPORTED_DAMAGE,
    "expected_consumption": _UNSUPPORTED_EXPECTED,
    "leather_allocation": _UNSUPPORTED_ALLOC,
    "employee_target_photo": _UNSUPPORTED_TARGET_PHOTO,
}
_LINING_UNSUPPORTED = {
    "damage_tracking": _UNSUPPORTED_DAMAGE,
    "expected_consumption": _UNSUPPORTED_EXPECTED,
    "lining_allocation": _UNSUPPORTED_ALLOC,
    "employee_target_photo": _UNSUPPORTED_TARGET_PHOTO,
    "lining_single_event": (
        "Lining is captured as a single LINING_CUTTING event per piece, so a "
        "piece's assigned and completed states coincide at stage grain. 'pending' "
        "is therefore measured against the lining-required population "
        "(needs_lining pieces not yet cut), not against a separate assignment "
        "record, which the schema does not model."
    ),
}
_STITCHING_UNSUPPORTED = {
    "damage_tracking": _UNSUPPORTED_DAMAGE,
    "employee_target_photo": _UNSUPPORTED_TARGET_PHOTO,
    "store_is_drawer_state": (
        "STORE is not a ProductionEvent — a piece never 'works' at STORE. Store "
        "handoff numbers are derived from the piece's DRAWER state "
        "(holding/received/sended), per core/store_display.py."
    ),
    "chain_order": (
        "The canonical pipeline (ProductionStage.leather_chain) sequences "
        "FUSING before PASTING; the requirements narrative lists Pasting then "
        "Fusing. 'total_received' uses the canonical chain (the sequence the "
        "production events actually follow)."
    ),
}
_STORE_UNSUPPORTED = {
    "empty_drawer_history": (
        "A drawer that has recycled to WAITING no longer carries its last "
        "style/material (current_piece_id is cleared). last_sent_date is the "
        "drawer's sended_at; last_style / last_material are null until a "
        "drawer_history table records prior occupants."
    ),
    "employee_photo": (
        "Employee has no photo column. Traceability returns the cutter's name / "
        "id; photo is null until an employee-profile image column exists."
    ),
    "movement_from_audit": (
        "Drawer movement history is reconstructed from audit_log rows for the "
        "drawer (DRAWER_RECEIVED / DRAWER_SENDED / MATERIAL_RECEIVED). "
        "Fine-grained 'leather added / lining added' steps appear only if the "
        "barcode two-door log wrote an audit row for them."
    ),
}


def _delay_status(*, deadline: date | None, ordered: int, completed: int,
                  today: date) -> str:
    if deadline is None:
        return "NO_DEADLINE"
    remaining = max(ordered - completed, 0)
    if remaining == 0:
        return "ON_TRACK"
    days_left = (deadline - today).days
    if days_left < 0:
        return "LATE"
    if days_left == 0:
        return "AT_RISK"
    required_per_day = remaining / days_left
    if required_per_day > remaining:      # <1 day of slack
        return "AT_RISK"
    return "ON_TRACK"


def _scope_label(client_scope: uuid.UUID | None) -> str:
    return "all_clients" if client_scope is None else f"client:{client_scope}"


def _drawer_status_label(state: str, leather_in: bool, lining_in: bool) -> str:
    """Map the drawer state machine to the doc's human status labels (§4)."""
    empty = not leather_in and not lining_in
    if state == "sended":
        return "Sent to Production"
    if state == "received":
        return "Ready to Send"
    if state in ("holding_leather", "holding_lining", "holding_both"):
        return "In Store"
    if state == "merged":
        return "Held"
    # waiting
    return "Empty" if empty else "In Store"


def _material_type(leather_in: bool, lining_in: bool) -> str:
    if leather_in and lining_in:
        return "LEATHER+LINING"
    if leather_in:
        return "LEATHER"
    if lining_in:
        return "LINING"
    return "NONE"


class DashboardService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = DashboardRepository(db)

    # ══════════════════════════════════════════════════════════════ CUTTING
    async def overview(
        self, *, client_scope: uuid.UUID | None,
        order_id: uuid.UUID | None = None, today: date | None = None,
    ) -> CuttingDashboard:
        today = today or date.today()

        prod = await self.repo.production_kpis(
            today=today, client_scope=client_scope, order_id=order_id)
        leather = await self.repo.leather_kpis(client_scope=client_scope)
        current = await self._current_order(
            client_scope=client_scope, order_id=order_id, today=today)
        cutters = await self._cutters(
            today=today, client_scope=client_scope, order_id=order_id)
        lots = await self._leather_lots()
        progress = await self._order_progress(client_scope=client_scope, today=today)
        daily = await self._daily(client_scope=client_scope)

        return CuttingDashboard(
            meta=DashboardMeta(generated_for=today, scope=_scope_label(client_scope),
                               unsupported=_CUTTING_UNSUPPORTED),
            production_kpis=ProductionKPIs(**prod),
            leather_kpis=LeatherKPIs(
                total_available_leather=leather["available"],
                consumed_leather=leather["consumed"],
                remaining_leather=leather["remaining"]),
            current_order=current,
            cutters=cutters,
            leather_lots=lots,
            order_progress=progress,
            daily_production=daily,
        )

    async def _current_order(
        self, *, client_scope, order_id, today,
    ) -> CurrentOrder | None:
        data = await self.repo.current_order(
            client_scope=client_scope, order_id=order_id)
        if data is None:
            return None
        by_style: dict[uuid.UUID, dict] = {}
        for sid, sname, article, thickness, op_code, cnt in data["rows"]:
            d = by_style.setdefault(sid, {
                "style_id": sid, "style_name": sname, "article": article,
                "thickness": thickness, "minted": 0, "completed": 0, "stages": {},
            })
            n = int(cnt)
            d["minted"] += n
            key = op_code or "UNSTARTED"
            d["stages"][key] = d["stages"].get(key, 0) + n
            if op_code == _TERMINAL:
                d["completed"] += n
        styles = [
            CurrentStyle(
                style_id=d["style_id"], style_name=d["style_name"],
                article=d["article"], thickness=d["thickness"],
                minted=d["minted"], completed=d["completed"],
                pending=max(d["minted"] - d["completed"], 0), stages=d["stages"])
            for d in by_style.values()
        ]
        total = sum(s.minted for s in styles)
        completed = sum(s.completed for s in styles)
        return CurrentOrder(
            order_id=data["order_id"], order_number=data["order_number"],
            client=data["client"], order_date=data["order_date"],
            delivery_deadline=data["delivery_deadline"],
            total_pieces=total, completed=completed,
            pending=max(total - completed, 0), styles=styles)

    async def _cutters(self, *, today, client_scope, order_id) -> list[CutterRow]:
        rows = await self.repo.cutter_performance(
            today=today, client_scope=client_scope, order_id=order_id)
        out: list[CutterRow] = []
        for eid, name, desig, assigned, assigned_today, events_today, consumed in rows:
            rework_today = max(int(events_today) - int(assigned_today), 0)
            out.append(CutterRow(
                employee_id=eid, name=name, designation=desig,
                assigned_pieces=int(assigned), assigned_today=int(assigned_today),
                rework_today=rework_today, consumed_leather=float(consumed or 0)))
        return out

    async def _leather_lots(self) -> list[LeatherLotRow]:
        rows = await self.repo.leather_by_lot()
        out: list[LeatherLotRow] = []
        for (lot_id, subtype, article, colour, thickness, uom,
             on_hand, consumed, pieces) in rows:
            out.append(LeatherLotRow(
                lot_id=lot_id, article=article, colour=colour, thickness=thickness,
                leather_type=subtype, uom=uom, available=float(on_hand or 0),
                consumed=float(consumed or 0), pieces_cut=int(pieces or 0),
                remaining=float(on_hand or 0)))
        return out

    async def _order_progress(self, *, client_scope, today) -> list[OrderProgressRow]:
        rows = await self.repo.order_progress(client_scope=client_scope)
        out: list[OrderProgressRow] = []
        for (oid, onum, odate, deadline, sid, sname, article,
             minted, completed, ordered) in rows:
            ordered_i, completed_i = int(ordered or 0), int(completed or 0)
            pct = round((completed_i / ordered_i * 100), 1) if ordered_i else 0.0
            out.append(OrderProgressRow(
                order_id=oid, order_number=onum, style_id=sid, style_name=sname,
                article=article, order_date=odate, delivery_deadline=deadline,
                total_ordered=ordered_i, minted=int(minted or 0),
                completed=completed_i, pending=max(ordered_i - completed_i, 0),
                completion_pct=pct,
                delay_status=_delay_status(deadline=deadline, ordered=ordered_i,
                                           completed=completed_i, today=today)))
        return out

    async def _daily(self, *, client_scope, days: int = 14) -> list[DailyRow]:
        end = date.today()
        start = end - timedelta(days=days - 1)
        rows = await self.repo.daily_production(
            start=start, end=end, client_scope=client_scope)
        return [DailyRow(work_date=wd, assigned=int(a or 0),
                         completed=int(c or 0), events=int(e or 0))
                for wd, a, c, e in rows]

    async def employee_pieces(
        self, *, employee_id: uuid.UUID, client_scope: uuid.UUID | None,
    ) -> list[EmployeePieceRow]:
        rows = await self.repo.pieces_for_employee(
            employee_id=employee_id, client_scope=client_scope)
        return [
            EmployeePieceRow(
                piece_code=code, seq=seq, size=size, colour=cname or ccode,
                style=style, current_stage=stage, last_worked=last)
            for code, seq, size, cname, ccode, style, stage, last in rows
        ]

    async def consumption(
        self, *, client_scope: uuid.UUID | None,
        order_id: uuid.UUID | None = None, employee_id: uuid.UUID | None = None,
        start: date | None = None, end: date | None = None,
    ) -> list[PieceConsumptionRow]:
        rows = await self.repo.piece_consumption(
            client_scope=client_scope, order_id=order_id, employee_id=employee_id,
            start=start, end=end)
        return [
            PieceConsumptionRow(
                piece_code=pcode, work_date=wd, employee=emp, stage=stage,
                actual_consumption=float(cons) if cons is not None else None,
                leather_article=lart, colour=lcol, thickness=lthk,
                style=style, order_number=onum, size=size)
            for (pcode, wd, emp, stage, cons, lart, lcol, lthk,
                 style, onum, size) in rows
        ]

    # ══════════════════════════════════════════════════════════════ LINING
    async def lining_overview(
        self, *, client_scope: uuid.UUID | None,
        order_id: uuid.UUID | None = None, today: date | None = None,
    ) -> LiningDashboard:
        today = today or date.today()

        prod = await self.repo.lining_production_kpis(
            today=today, client_scope=client_scope, order_id=order_id)
        material = await self.repo.lining_kpis(client_scope=client_scope)
        current = await self._current_order(
            client_scope=client_scope, order_id=order_id, today=today)
        employees = await self._lining_employees(
            today=today, client_scope=client_scope, order_id=order_id)
        lots = await self._lining_lots()
        progress = await self._order_progress(client_scope=client_scope, today=today)
        daily = await self._lining_daily(client_scope=client_scope)
        upcoming = await self._lining_upcoming(
            client_scope=client_scope, order_id=order_id)

        return LiningDashboard(
            meta=DashboardMeta(generated_for=today, scope=_scope_label(client_scope),
                               unsupported=_LINING_UNSUPPORTED),
            production_kpis=LiningProductionKPIs(**prod),
            material_kpis=LiningMaterialKPIs(
                total_available_lining=material["available"],
                used_lining=material["consumed"],
                remaining_lining=material["remaining"]),
            current_order=current,
            employees=employees,
            lining_lots=lots,
            order_progress=progress,
            daily_production=daily,
            upcoming=upcoming,
        )

    async def _lining_employees(
        self, *, today, client_scope, order_id,
    ) -> list[LiningEmployeeRow]:
        rows = await self.repo.lining_employees(
            today=today, client_scope=client_scope, order_id=order_id)
        out: list[LiningEmployeeRow] = []
        for eid, name, desig, assigned, assigned_today, events_today, consumed in rows:
            rework_today = max(int(events_today) - int(assigned_today), 0)
            out.append(LiningEmployeeRow(
                employee_id=eid, name=name, designation=desig,
                assigned_pieces=int(assigned), assigned_today=int(assigned_today),
                completed_today=int(assigned_today), rework_today=rework_today,
                used_lining=float(consumed or 0)))
        return out

    async def _lining_lots(self) -> list[LiningLotRow]:
        rows = await self.repo.lining_by_lot()
        out: list[LiningLotRow] = []
        for (lot_id, subtype, article, colour, thickness, uom,
             on_hand, used, pieces) in rows:
            out.append(LiningLotRow(
                lot_id=lot_id, lining_type=subtype, article=article, colour=colour,
                thickness=thickness, uom=uom, available=float(on_hand or 0),
                used=float(used or 0), pieces_lined=int(pieces or 0),
                remaining=float(on_hand or 0)))
        return out

    async def _lining_daily(self, *, client_scope, days: int = 14) -> list[DailyRow]:
        end = date.today()
        start = end - timedelta(days=days - 1)
        rows = await self.repo.daily_production(
            start=start, end=end, client_scope=client_scope,
            assigned_stages=(_LINING_CUT,), completed_stage=_LINING_CUT)
        return [DailyRow(work_date=wd, assigned=int(a or 0),
                         completed=int(c or 0), events=int(e or 0))
                for wd, a, c, e in rows]

    async def _lining_upcoming(
        self, *, client_scope, order_id,
    ) -> list[UpcomingPieceRow]:
        rows = await self.repo.lining_upcoming(
            client_scope=client_scope, order_id=order_id)
        return [
            UpcomingPieceRow(
                order_id=oid, order_number=onum, style_id=sid, style=sname,
                article=article, colour=colour, thickness=thickness, size=size,
                expected_qty=int(qty or 0), target_date=deadline,
                cutting_status="DONE", lining_status="PENDING")
            for (oid, onum, deadline, sid, sname, article, thickness,
                 colour, size, qty) in rows
        ]

    async def lining_consumption(
        self, *, client_scope: uuid.UUID | None,
        order_id: uuid.UUID | None = None, employee_id: uuid.UUID | None = None,
        start: date | None = None, end: date | None = None,
    ) -> list[PieceConsumptionRow]:
        from app.modules.production.models import ProductionEvent
        rows = await self.repo.piece_consumption(
            client_scope=client_scope, order_id=order_id, employee_id=employee_id,
            start=start, end=end, stages=(_LINING_CUT,),
            material_lot_col=ProductionEvent.lining_lot_id)
        return [
            PieceConsumptionRow(
                piece_code=pcode, work_date=wd, employee=emp, stage=stage,
                actual_consumption=float(cons) if cons is not None else None,
                leather_article=lart, colour=lcol, thickness=lthk,
                style=style, order_number=onum, size=size)
            for (pcode, wd, emp, stage, cons, lart, lcol, lthk,
                 style, onum, size) in rows
        ]

    # ══════════════════════════════════════════════════════════════ STITCHING
    async def stitching_overview(
        self, *, client_scope: uuid.UUID | None,
        order_id: uuid.UUID | None = None, today: date | None = None,
    ) -> StitchingDashboard:
        today = today or date.today()

        funnel = await self.repo.stitching_funnel(
            today=today, client_scope=client_scope, order_id=order_id)
        rework = await self.repo.stitching_rework(
            today=today, client_scope=client_scope, order_id=order_id)
        handoff = await self.repo.store_handoff(client_scope=client_scope)
        assigned = await self.repo._assigned_count(
            today=today, client_scope=client_scope, order_id=order_id,
            stages=tuple(_STAGE_ORDER))
        progress = await self._order_progress(client_scope=client_scope, today=today)
        current = await self._current_order(
            client_scope=client_scope, order_id=order_id, today=today)
        cur_style = await self._stitching_current_style(
            client_scope=client_scope, order_id=order_id)
        employees = await self._stitching_employees(
            today=today, client_scope=client_scope, order_id=order_id)
        daily = await self._stitching_daily(client_scope=client_scope)

        def ov(code: str) -> int:
            return funnel.get(code, {}).get("overall", 0)

        def td(code: str) -> int:
            return funnel.get(code, {}).get("today", 0)

        # received-per-stage from the canonical predecessor chain
        line_ready = max(handoff["sended"] - ov(_LINE), 0)
        received = {
            _PASTING: ov(_FUSING),
            _FUSING: ov(_LEATHER_CUT),
            _LINE: ov(_LINE) + line_ready,
            _SHELL: ov(_LINE),
            _FINAL: ov(_SHELL),
        }

        stages: list[StageBlock] = []
        for code in _STAGE_ORDER:
            label, section = _STAGE_META[code]
            rec = received.get(code, 0)
            completed = ov(code)
            stages.append(StageBlock(
                stage=code, label=label, section=section,
                total_received=rec, assigned_pieces=rec, completed_pieces=completed,
                pending_pieces=max(rec - completed, 0),
                rework_pieces=rework.get(code, 0),
                daily_completed=td(code)))

        ready_for_inspection = max(ov(_FINAL) - ov(_INSPECTION), 0)
        overall_completed = ov(_FINAL)
        kpis = StitchingKPIs(
            overall_pieces=sum(p.total_ordered for p in progress),
            assigned_pieces=assigned["overall"],
            completed_today=td(_FINAL),
            overall_completed=overall_completed,
            pending_today=max(assigned["today"] - td(_FINAL), 0),
            overall_pending=max(assigned["overall"] - overall_completed, 0),
            rework_pieces=sum(rework.values()),
            ready_for_store=handoff["holding"],
            in_store=handoff["holding"] + handoff["received"],
            ready_for_inspection=ready_for_inspection,
            target_date=current.delivery_deadline if current else None)

        store_handoff = StoreHandoff(
            ready_for_store=handoff["holding"],
            in_store=handoff["holding"] + handoff["received"],
            sent_to_store=handoff["sended"],
            in_drawer=handoff["in_drawer"],
            ready_for_stitching=line_ready,
            store_pending=handoff["holding"] + handoff["received"])

        return StitchingDashboard(
            meta=DashboardMeta(generated_for=today, scope=_scope_label(client_scope),
                               unsupported=_STITCHING_UNSUPPORTED),
            kpis=kpis, stages=stages, store_handoff=store_handoff,
            current_style=cur_style, employees=employees, daily_production=daily,
            order_progress=progress)

    async def _stitching_current_style(
        self, *, client_scope, order_id,
    ) -> StitchingCurrentStyle | None:
        data = await self.repo.stitching_current_style_funnel(
            client_scope=client_scope, order_id=order_id)
        if data is None:
            return None
        # aggregate per style, then pick the running style (largest piece count)
        per_style: dict[uuid.UUID, dict] = {}
        for sid, sname, article, op_code, cnt in data["rows"]:
            s = per_style.setdefault(
                sid, {"name": sname, "article": article, "ops": {}})
            s["ops"][op_code] = int(cnt or 0)
        if not per_style:
            return None
        totals = data["totals"]
        top_sid = max(per_style, key=lambda k: totals.get(k, 0))
        s = per_style[top_sid]
        stage_rows = [
            StitchingStyleStage(
                stage=code, label=_FUNNEL_LABELS.get(code, code),
                section=_STAGE_META.get(code, ("", "CUT"))[1],
                count=s["ops"].get(code, 0))
            for code in _FUNNEL_PIPELINE
        ]
        ready_for_inspection = max(
            s["ops"].get(_FINAL, 0) - s["ops"].get(_INSPECTION, 0), 0)
        return StitchingCurrentStyle(
            style_id=top_sid, style=s["name"], article=s["article"],
            order_id=data["order_id"], order_number=data["order_number"],
            total_pieces=totals.get(top_sid, 0), stages=stage_rows,
            ready_for_inspection=ready_for_inspection)

    async def _stitching_employees(
        self, *, today, client_scope, order_id,
    ) -> list[StitchingEmployeeRow]:
        rows = await self.repo.stitching_employees(
            today=today, client_scope=client_scope, order_id=order_id)
        out: list[StitchingEmployeeRow] = []
        for eid, name, desig, op_code, assigned, assigned_today, events_today, _events in rows:
            label, section = _STAGE_META.get(op_code, (op_code, "POST_STORE"))
            rework_today = max(int(events_today) - int(assigned_today), 0)
            out.append(StitchingEmployeeRow(
                employee_id=eid, name=name, designation=desig,
                stage=op_code, section=section,
                assigned_pieces=int(assigned), completed_pieces=int(assigned),
                assigned_today=int(assigned_today),
                completed_today=int(assigned_today), rework_today=rework_today))
        return out

    async def _stitching_daily(self, *, client_scope, days: int = 14) -> list[StageDailyRow]:
        end = date.today()
        start = end - timedelta(days=days - 1)
        rows = await self.repo.stitching_daily(
            start=start, end=end, client_scope=client_scope)
        return [StageDailyRow(work_date=wd, stage=code,
                              completed=int(c or 0), events=int(e or 0))
                for wd, code, c, e in rows]

    async def piece_trace(self, *, piece_code: str) -> PieceTrace | None:
        """§21 traceability — full stage history + the derived STORE overlay."""
        data = await self.repo.piece_stage_history(piece_code=piece_code)
        if data is None:
            return None

        disp = display_stage(
            current_event_stage=data["current_stage"],
            drawer_state=data["drawer_state"],
            needs_lining=data["needs_lining"])

        history: list[PieceStageHistoryRow] = []
        inserted_store = False
        for op_code, emp, wd in data["events"]:
            label = _FUNNEL_LABELS.get(op_code, (op_code or "").replace("_", " ").title())
            history.append(PieceStageHistoryRow(
                stage=op_code, label=label, employee=emp, work_date=wd))
            # insert the STORE overlay row right after the cut-side terminals
            if disp["in_store"] and not inserted_store and op_code in (_PASTING, _LINING_CUT):
                history.append(PieceStageHistoryRow(
                    stage=STORE, label="Store / Drawer", employee=None,
                    work_date=None, is_store_overlay=True,
                    store_status=disp["store_status"]))
                inserted_store = True
        if disp["in_store"] and not inserted_store:
            history.append(PieceStageHistoryRow(
                stage=STORE, label="Store / Drawer", employee=None, work_date=None,
                is_store_overlay=True, store_status=disp["store_status"]))

        return PieceTrace(
            piece_code=data["piece_code"], style=data["style"],
            order_number=data["order_number"], colour=data["colour"],
            size=data["size"], display_stage=disp["display_stage"],
            in_store=disp["in_store"], store_label=disp["label"], history=history)

    # ══════════════════════════════════════════════════════════════ STORE
    async def store_overview(
        self, *, client_scope: uuid.UUID | None,
        style_id: uuid.UUID | None = None, state: str | None = None,
        material_type: str | None = None, today: date | None = None,
    ) -> StoreDashboard:
        today = today or date.today()

        kpis = await self.repo.store_kpis(client_scope=client_scope)
        current_styles = await self._store_current_styles(client_scope=client_scope)
        grid = await self.repo.drawer_grid(
            client_scope=client_scope, style_id=style_id, state=state,
            material_type=material_type)

        drawers = [self._drawer_row(r) for r in grid]
        held = [d for d in drawers if d.state == "received"]
        empty = [
            EmptyDrawerRow(
                drawer_id=d.drawer_id, drawer_code=d.drawer_code, seq=d.seq,
                last_style=None, last_material=None,
                last_sent_date=d.sended_at, availability="Yes")
            for d in drawers
            if not d.leather_in and not d.lining_in
        ]

        return StoreDashboard(
            meta=DashboardMeta(generated_for=today, scope=_scope_label(client_scope),
                               unsupported=_STORE_UNSUPPORTED),
            kpis=StoreKPIs(**kpis), current_styles=current_styles,
            drawers=drawers, held_drawers=held, empty_drawers=empty)

    def _drawer_row(self, r) -> DrawerRow:
        (did, code, seq, state, leather_in, lining_in, received_at, sended_at,
         created_at, pid, pcode, sid, sname, oid, onum, deadline,
         colour, size) = r
        return DrawerRow(
            drawer_id=did, drawer_code=code, seq=seq, state=state,
            status_label=_drawer_status_label(state, bool(leather_in), bool(lining_in)),
            contents=holding_label(leather_in=leather_in, lining_in=lining_in),
            material_type=_material_type(bool(leather_in), bool(lining_in)),
            leather_in=bool(leather_in), lining_in=bool(lining_in),
            piece_id=pid, piece_code=pcode, style_id=sid, style=sname,
            order_id=oid, order_number=onum, colour=colour, size=size,
            received_at=received_at, sended_at=sended_at, target_date=deadline)

    async def _store_current_styles(
        self, *, client_scope,
    ) -> list[StoreCurrentStyleRow]:
        rows = await self.repo.store_current_styles(client_scope=client_scope)
        return [
            StoreCurrentStyleRow(
                style_id=sid, style=sname, order_id=oid, order_number=onum,
                drawers=int(drawers or 0), leather_drawers=int(leather or 0),
                lining_drawers=int(lining or 0), both_drawers=int(both or 0),
                ready_to_send=int(ready or 0), target_date=deadline)
            for (sid, sname, oid, onum, deadline, drawers, leather, lining,
                 both, ready) in rows
        ]

    async def drawer_detail(self, *, drawer_id: uuid.UUID) -> DrawerDetail | None:
        head = await self.repo.drawer_detail(drawer_id=drawer_id)
        if head is None:
            return None
        cutters: list[DrawerCutterRow] = []
        if head["piece_id"] is not None:
            for op_code, emp_id, emp_name, wd in await self.repo.drawer_cutters(
                    piece_id=head["piece_id"]):
                cutters.append(DrawerCutterRow(
                    material_type="LEATHER" if op_code == _LEATHER_CUT else "LINING",
                    employee_id=emp_id, employee=emp_name, work_date=wd))
        return DrawerDetail(
            drawer_id=head["drawer_id"], drawer_code=head["code"], seq=head["seq"],
            state=head["state"],
            status_label=_drawer_status_label(
                head["state"], bool(head["leather_in"]), bool(head["lining_in"])),
            contents=holding_label(
                leather_in=head["leather_in"], lining_in=head["lining_in"]),
            piece_id=head["piece_id"], piece_code=head["piece_code"],
            style=head["style"], order_number=head["order_number"],
            colour=head["colour"], size=head["size"],
            leather_in=bool(head["leather_in"]), lining_in=bool(head["lining_in"]),
            date_received=head["received_at"], date_sended=head["sended_at"],
            created_at=head["created_at"], cutters=cutters)

    async def drawer_movement(self, *, drawer_id: uuid.UUID) -> list[DrawerMovementRow]:
        rows = await self.repo.drawer_movement(drawer_id=drawer_id)
        return [
            DrawerMovementRow(action=action, at=at or created_at,
                              actor_user_id=actor)
            for action, at, actor, created_at in rows
        ]

    async def material_traceability(
        self, *, client_scope: uuid.UUID | None,
        piece_code: str | None = None, style_id: uuid.UUID | None = None,
        material_type: str | None = None,
    ) -> list[MaterialCutterTrace]:
        rows = await self.repo.material_cutter_trace(
            client_scope=client_scope, piece_code=piece_code, style_id=style_id,
            material_type=material_type)
        return [
            MaterialCutterTrace(
                piece_code=pcode,
                material_type="LEATHER" if op_code == _LEATHER_CUT else "LINING",
                employee_id=emp_id, employee=emp_name, style=style,
                order_number=onum, colour=colour, size=size,
                cutting_date=wd, drawer_code=dcode)
            for (pcode, op_code, emp_id, emp_name, style, onum, colour, size,
                 wd, dcode) in rows
        ]
