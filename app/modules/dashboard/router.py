"""
================================================================================
modules/dashboard/router.py — Manager Dashboards HTTP surface (READ-ONLY)
================================================================================
CUTTING    GET /dashboard/cutting                          composite (Cutting §18)
           GET /dashboard/cutting/employees/{employee_id}  cutter → pieces (§5)
           GET /dashboard/cutting/consumption              per-piece grid (§11/§14)

LINING     GET /dashboard/lining                           composite (Lining §18/§20)
           GET /dashboard/lining/employees/{employee_id}   worker → pieces (§17)
           GET /dashboard/lining/consumption               per-piece lining (§13)

STITCHING  GET /dashboard/stitching                        composite (Stitching §3)
           GET /dashboard/stitching/employees/{employee_id} worker → pieces (§14)
           GET /dashboard/stitching/pieces/{piece_code}     piece trace (§21)

STORE      GET /dashboard/store                             composite (Store §18/§20)
           GET /dashboard/store/drawers/{drawer_id}         drawer detail (§6)
           GET /dashboard/store/drawers/{drawer_id}/movement movement history (§17)
           GET /dashboard/store/traceability                who-cut-what (§8)

ROLE
    These are managers' screens plus the office roles that read across the floor.
    MD/DM/HR are superusers (require_roles bypasses them). A CLIENT token is NOT
    admitted to floor dashboards — they expose worker names + floor data.

TENANCY
    `client_scope` mirrors production/router.py exactly: None for staff (read
    across clients), the caller's own client_id for a CLIENT login. Kept on every
    handler so the same code serves a future scoped read without a second path —
    and a cross-tenant id yields empty, never another client's data.

READ-ONLY: no request bodies, no writes, no state transitions.
================================================================================
"""
from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.enums import UserRole
from app.modules.dashboard.schemas import (
    CuttingDashboard, DrawerDetail, DrawerMovementRow, EmployeePieceRow,
    LiningDashboard, MaterialCutterTrace, PieceConsumptionRow, PieceTrace,
    StitchingDashboard, StoreDashboard,
)
from app.modules.dashboard.service import DashboardService
from app.modules.users.deps import get_current_user, require_roles
from app.modules.users.models import User

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])

# Office + floor-side readers. MD/DM bypass via require_roles superuser set.
_DASHBOARD_READERS = require_roles(
    UserRole.MANAGING_DIRECTOR, UserRole.DIRECT_MANAGER, UserRole.HR,
    UserRole.SUPERVISOR, UserRole.CUTTING_MANAGER, UserRole.STITCHING_MANAGER,
    UserRole.LINING_MANAGER,
)


def client_scope(user: User = Depends(get_current_user)) -> uuid.UUID | None:
    """Tenancy: a CLIENT login is scoped to its own client_id; staff read across
    all clients (None). Mirrors production/analytics."""
    return user.client_id if user.role == UserRole.CLIENT else None


# ══════════════════════════════════════════════════════════════════ CUTTING
@router.get("/cutting", response_model=CuttingDashboard)
async def cutting_dashboard(
    order_id: uuid.UUID | None = Query(
        None, description="Pin the 'current order' block to a specific order; "
                          "defaults to the most recent order in scope."),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_DASHBOARD_READERS),
    scope: uuid.UUID | None = Depends(client_scope),
):
    """The complete Cutting Manager Dashboard in one call (~11 grouped queries)."""
    return await DashboardService(db).overview(client_scope=scope, order_id=order_id)


@router.get("/cutting/employees/{employee_id}", response_model=list[EmployeePieceRow])
async def cutting_employee_pieces(
    employee_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_DASHBOARD_READERS),
    scope: uuid.UUID | None = Depends(client_scope),
):
    """Cutting §5 drill-down: which pieces a cutter cut, with stage + last-worked."""
    return await DashboardService(db).employee_pieces(
        employee_id=employee_id, client_scope=scope)


@router.get("/cutting/consumption", response_model=list[PieceConsumptionRow])
async def cutting_consumption(
    order_id: uuid.UUID | None = Query(None),
    employee_id: uuid.UUID | None = Query(None),
    start: date | None = Query(None),
    end: date | None = Query(None),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_DASHBOARD_READERS),
    scope: uuid.UUID | None = Depends(client_scope),
):
    """Cutting §11/§14: one row per leather-cut event with actual consumption."""
    return await DashboardService(db).consumption(
        client_scope=scope, order_id=order_id, employee_id=employee_id,
        start=start, end=end)


# ══════════════════════════════════════════════════════════════════ LINING
@router.get("/lining", response_model=LiningDashboard)
async def lining_dashboard(
    order_id: uuid.UUID | None = Query(
        None, description="Pin the current-order block; defaults to most recent."),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_DASHBOARD_READERS),
    scope: uuid.UUID | None = Depends(client_scope),
):
    """The complete Lining Manager Dashboard in one call (~12 grouped queries):
    production + lining-material KPIs, current order, per-worker lining
    performance, lining lots, per-order progress, 14-day trend, and upcoming
    lining work (leather-cut pieces not yet lining-cut)."""
    return await DashboardService(db).lining_overview(
        client_scope=scope, order_id=order_id)


@router.get("/lining/employees/{employee_id}", response_model=list[EmployeePieceRow])
async def lining_employee_pieces(
    employee_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_DASHBOARD_READERS),
    scope: uuid.UUID | None = Depends(client_scope),
):
    """Lining §17 drill-down: the pieces a lining worker touched, with stage."""
    return await DashboardService(db).employee_pieces(
        employee_id=employee_id, client_scope=scope)


@router.get("/lining/consumption", response_model=list[PieceConsumptionRow])
async def lining_consumption(
    order_id: uuid.UUID | None = Query(None),
    employee_id: uuid.UUID | None = Query(None),
    start: date | None = Query(None),
    end: date | None = Query(None),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_DASHBOARD_READERS),
    scope: uuid.UUID | None = Depends(client_scope),
):
    """Lining §13: one row per lining-cut event with actual lining consumption
    (the lot joined via lining_lot_id). Expected/variance/waste null (BOM)."""
    return await DashboardService(db).lining_consumption(
        client_scope=scope, order_id=order_id, employee_id=employee_id,
        start=start, end=end)


# ══════════════════════════════════════════════════════════════════ STITCHING
@router.get("/stitching", response_model=StitchingDashboard)
async def stitching_dashboard(
    order_id: uuid.UUID | None = Query(
        None, description="Pin the current-style block; defaults to most recent."),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_DASHBOARD_READERS),
    scope: uuid.UUID | None = Depends(client_scope),
):
    """The complete Stitching Manager Dashboard in one call (~10 grouped queries):
    top KPIs, the pre-store (Pasting/Fusing) and post-store (Line/Shell/Final)
    stage blocks, the store handoff, the running style's stage funnel, per-stage
    employee performance, the 14-day per-stage trend, and per-order progress."""
    return await DashboardService(db).stitching_overview(
        client_scope=scope, order_id=order_id)


@router.get("/stitching/employees/{employee_id}", response_model=list[EmployeePieceRow])
async def stitching_employee_pieces(
    employee_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_DASHBOARD_READERS),
    scope: uuid.UUID | None = Depends(client_scope),
):
    """Stitching §14 drill-down: the pieces a worker touched, with current stage."""
    return await DashboardService(db).employee_pieces(
        employee_id=employee_id, client_scope=scope)


@router.get("/stitching/pieces/{piece_code}", response_model=PieceTrace)
async def stitching_piece_trace(
    piece_code: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_DASHBOARD_READERS),
):
    """Stitching §21 traceability: a piece's full stage history in pipeline order,
    plus the derived STORE overlay (from its drawer state). 404-safe: returns an
    empty trace body only when the piece exists; unknown codes 422/None-guarded."""
    trace = await DashboardService(db).piece_trace(piece_code=piece_code)
    if trace is None:
        from fastapi import HTTPException, status
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Piece not found")
    return trace


# ══════════════════════════════════════════════════════════════════ STORE
# The Store dashboard is the STORE_MANAGER's screen. There is no dedicated
# STORE_MANAGER role in the enum yet, so DM/MD (superusers) + the floor managers
# who hand off to / from the store read it. When a STORE_MANAGER role is added,
# include it here — the handler is already role-agnostic.
@router.get("/store", response_model=StoreDashboard)
async def store_dashboard(
    style_id: uuid.UUID | None = Query(None, description="§14 style-wise filter."),
    state: str | None = Query(
        None, description="§4 drawer-state filter (waiting|merged|holding_leather|"
                          "holding_lining|holding_both|received|sended)."),
    material_type: str | None = Query(
        None, description="§15 material filter: LEATHER | LINING | BOTH."),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_DASHBOARD_READERS),
    scope: uuid.UUID | None = Depends(client_scope),
):
    """The complete Store Manager Dashboard in one call (~4 grouped queries):
    drawer KPIs, current styles in store, the filterable drawer grid, held
    drawers, and empty drawers available for reuse."""
    return await DashboardService(db).store_overview(
        client_scope=scope, style_id=style_id, state=state,
        material_type=material_type)


@router.get("/store/drawers/{drawer_id}", response_model=DrawerDetail)
async def store_drawer_detail(
    drawer_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_DASHBOARD_READERS),
):
    """Store §6 — full drawer info + who cut the leather / lining it holds."""
    detail = await DashboardService(db).drawer_detail(drawer_id=drawer_id)
    if detail is None:
        from fastapi import HTTPException, status
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Drawer not found")
    return detail


@router.get("/store/drawers/{drawer_id}/movement", response_model=list[DrawerMovementRow])
async def store_drawer_movement(
    drawer_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_DASHBOARD_READERS),
):
    """Store §17 — a drawer's movement history from the audit trail."""
    return await DashboardService(db).drawer_movement(drawer_id=drawer_id)


@router.get("/store/traceability", response_model=list[MaterialCutterTrace])
async def store_traceability(
    piece_code: str | None = Query(None),
    style_id: uuid.UUID | None = Query(None),
    material_type: str | None = Query(None, description="LEATHER | LINING"),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_DASHBOARD_READERS),
    scope: uuid.UUID | None = Depends(client_scope),
):
    """Store §8 — who cut the leather/lining for each piece, and which drawer
    holds it. Filterable by piece code, style, or material type."""
    return await DashboardService(db).material_traceability(
        client_scope=scope, piece_code=piece_code, style_id=style_id,
        material_type=material_type)
