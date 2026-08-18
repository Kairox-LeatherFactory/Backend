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

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.enums import ProductionStage, UserRole
from app.modules.dashboard.schemas import (
    CuttingDashboard, DirectManagerDashboard, DrawerDetail, DrawerMovementRow,
    EmployeePieceRow, LiningDashboard, MaterialCutterTrace, OrderTracking,
    PieceConsumptionRow, PieceTrace, StitchingDashboard, StoreDashboard,
    StyleTracking,
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
    # The store dashboard is this role's screen. The note below the STORE section
    # asked for it to be added once a STORE_MANAGER role existed; it now does.
    UserRole.STORE_MANAGER,
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


# The only two stages that record material consumption. A consumption grid for
# any other stage is a question with no answer, so asking for one is a 422 rather
# than a silently empty list.
_CONSUMPTION_STAGES = (ProductionStage.LEATHER_CUTTING.value,
                       ProductionStage.LINING_CUTTING.value)


def _validated_stage(stage: str | None, default: str) -> str:
    if stage is None:
        return default
    norm = stage.strip().upper()
    if norm not in _CONSUMPTION_STAGES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"stage must be one of {list(_CONSUMPTION_STAGES)} — only the cut "
            f"stages record material consumption.")
    return norm


@router.get("/cutting/consumption", response_model=list[PieceConsumptionRow])
async def cutting_consumption(
    order_id: uuid.UUID | None = Query(None),
    employee_id: uuid.UUID | None = Query(None),
    start: date | None = Query(None),
    end: date | None = Query(None),
    stage: str | None = Query(
        None, description="LEATHER_CUTTING (default) or LINING_CUTTING. Rows never "
                          "mix stages: the lot column follows the stage."),
    include_unmeasured: bool = Query(
        False, description="Include cut events with no recorded quantity "
                           "(actual_consumption: null) instead of omitting them."),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_DASHBOARD_READERS),
    scope: uuid.UUID | None = Depends(client_scope),
):
    """Cutting §11/§14: one row per leather-cut event with actual consumption.

    ONE STAGE PER RESPONSE. This previously returned BOTH cut stages while joining
    the leather lot, so lining-cut events appeared here with a blank lot and their
    quantities counted toward leather totals."""
    return await DashboardService(db).consumption(
        client_scope=scope, order_id=order_id, employee_id=employee_id,
        start=start, end=end,
        stage=_validated_stage(stage, ProductionStage.LEATHER_CUTTING.value),
        include_unmeasured=include_unmeasured)


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
    stage: str | None = Query(
        None, description="LINING_CUTTING (default) or LEATHER_CUTTING. Rows never "
                          "mix stages: the lot column follows the stage."),
    include_unmeasured: bool = Query(
        False, description="Include lining cuts logged with no quantity. Lining "
                           "consumption is optional, so these are real work that "
                           "would otherwise be invisible here."),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_DASHBOARD_READERS),
    scope: uuid.UUID | None = Depends(client_scope),
):
    """Lining §13: one row per lining-cut event with actual lining consumption
    (the lot joined via lining_lot_id). Expected/variance/waste null (BOM).

    Lining consumption is OPTIONAL, so a cut logged without a measurement is
    excluded by default — pass `include_unmeasured=true` to see those events with
    a null quantity rather than not at all."""
    return await DashboardService(db).lining_consumption(
        client_scope=scope, order_id=order_id, employee_id=employee_id,
        start=start, end=end,
        stage=_validated_stage(stage, ProductionStage.LINING_CUTTING.value),
        include_unmeasured=include_unmeasured)


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


# ══════════════════════════════════════════════════ PIECE TRACKING (all stages)
# ONE HANDLER, SEVERAL URLS.
#
# Piece-level tracking existed only for stitching, so the cutting, lining and
# store screens had no way to follow a single garment. The read itself was never
# stitching-specific — it walks the whole pipeline — it was just mounted once.
#
# Four aliases rather than four implementations: separate per-stage endpoints
# would drift the moment one of them gained a field, and a piece's history is the
# same history whichever screen is asking. `/dashboard/pieces/{code}` is the
# canonical route; the per-stage paths exist so each dashboard can keep its own
# URL namespace, and the original stitching path keeps working unchanged.
async def _piece_trace(db: AsyncSession, piece_code: str) -> PieceTrace:
    trace = await DashboardService(db).piece_trace(piece_code=piece_code)
    if trace is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"No piece with code '{piece_code}'. Scan the barcode again, or check "
            f"whether the label is a drawer or employee card rather than a piece.")
    return trace


@router.get("/pieces/{piece_code}", response_model=PieceTrace)
async def piece_trace(
    piece_code: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_DASHBOARD_READERS),
):
    """A piece's full stage history in pipeline order, with the derived STORE
    overlay, its drawer, and what it consumed at each cut.

    THE canonical piece-tracking read. Two neighbours answer different questions:
    `/production/piece-state` says what happens NEXT (and which stage cards to
    lock); `/analytics/pieces/{code}/story` is the analytics life story.
    """
    return await _piece_trace(db, piece_code)


@router.get("/cutting/pieces/{piece_code}", response_model=PieceTrace)
async def cutting_piece_trace(
    piece_code: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_DASHBOARD_READERS),
):
    """Piece tracking from the Cutting dashboard. Same body as
    `/dashboard/pieces/{piece_code}` — leather consumption per cut event is on
    each history row."""
    return await _piece_trace(db, piece_code)


@router.get("/lining/pieces/{piece_code}", response_model=PieceTrace)
async def lining_piece_trace(
    piece_code: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_DASHBOARD_READERS),
):
    """Piece tracking from the Lining dashboard. A lining cut logged without a
    measurement still appears here, with a null consumption."""
    return await _piece_trace(db, piece_code)


@router.get("/store/pieces/{piece_code}", response_model=PieceTrace)
async def store_piece_trace(
    piece_code: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_DASHBOARD_READERS),
):
    """Piece tracking from the Store dashboard — carries the drawer code, what it
    is holding, and where it was sent."""
    return await _piece_trace(db, piece_code)


@router.get("/stitching/pieces/{piece_code}", response_model=PieceTrace)
async def stitching_piece_trace(
    piece_code: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_DASHBOARD_READERS),
):
    """Stitching §21 traceability. The original route, unchanged — the same body
    is now also served under `/dashboard/pieces/{piece_code}`."""
    return await _piece_trace(db, piece_code)


# ══════════════════════════════════════════════════════════════════ STORE
# The Store dashboard is the STORE_MANAGER's screen, and that role now exists and
# is in _DASHBOARD_READERS above. DM/MD (superusers) and the floor managers who
# hand off to / from the store read it too.
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


# ══════════════════════════════════════════════════════════ DIRECT MANAGER
# The factory-wide control panel. The DM is a superuser and bypasses
# require_roles; MD/HR are admitted explicitly through _DASHBOARD_READERS. It is
# factory-wide, so scope is passed through and is None for staff (a scoped CLIENT
# still gets its own slice rather than a different code path).
@router.get("/alerts")
async def factory_alerts(
    today: date | None = Query(None, description="Override 'today' for freight risk."),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_DASHBOARD_READERS),
    scope: uuid.UUID | None = Depends(client_scope),
):
    """THE ALERTS, FOR EVERY MANAGER (change-list item 1).

    Replaces the standalone Analytics Overview + Risk Alerts screens, which are
    now 410 Gone. The bottleneck and the alerts were the only parts of those
    pages anyone acted on, and a manager should not have to know a separate
    screen exists to find out their line is blocked — so they moved here, onto
    the surface every manager already opens, readable by every manager role
    rather than the DM alone.

    Three blocks, most-actionable first:

      bottleneck     THE DEEPEST QUEUE — the stage with the most work waiting in
                     front of it. Not "the first unfinished stage": the first
                     unfinished stage is wherever the line happens to have got to,
                     which is not a constraint and not actionable.
      stage_spread   Where each downstream stage lags the leather cut. The gap is
                     true WIP in flight — cut but not yet arrived at stage X.
      freight_risk   Orders approaching their sea cut-off. Missing it means air
                     freight, which is the single biggest margin event in the
                     business, so it is an alert and not a report line.

    `alert_count` is what a badge should render. Empty lists are a good day, not
    a missing feature."""
    from app.modules.analytics.service import AnalyticsService

    analytics = AnalyticsService(db)
    dm = await DashboardService(db).direct_manager_overview(client_scope=scope)
    bottleneck = dm.get("bottleneck") if isinstance(dm, dict) else getattr(
        dm, "bottleneck", None)
    spread = await analytics.stage_spread_alerts(client_scope=scope)
    freight = await analytics.freight_risk(today, client_scope=scope)
    return {
        "bottleneck": bottleneck,
        "stage_spread": spread,
        "freight_risk": freight,
        "alert_count": len(spread) + len(freight),
    }


@router.get("/direct-manager", response_model=DirectManagerDashboard)
async def direct_manager_dashboard(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_DASHBOARD_READERS),
    scope: uuid.UUID | None = Depends(client_scope),
):
    """The complete Direct Manager dashboard in one call (~15 grouped queries):
    overall production, department performance, the stage pipeline + bottleneck,
    production rate, quality, attendance, store send/receive, per-order progress
    and the 14-day trend.

    Composed from the four stage dashboards' own aggregates, so this screen and
    /cutting, /lining, /stitching, /store can never disagree. Drill into any of
    them for stage detail. Read `meta.unsupported` — several quality and costing
    figures are null by design because no table backs them yet."""
    return await DashboardService(db).direct_manager_overview(client_scope=scope)


@router.get("/direct-manager/orders/{order_id}", response_model=OrderTracking)
async def dm_order_tracking(
    order_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_DASHBOARD_READERS),
    scope: uuid.UUID | None = Depends(client_scope),
):
    """Order tracking — an order's complete production journey across every
    stage, with the stage currently holding the largest backlog."""
    tracking = await DashboardService(db).dm_order_tracking(
        order_id=order_id, client_scope=scope)
    if tracking is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"No order with id {order_id}.")
    return tracking


@router.get("/direct-manager/styles/{style_id}", response_model=StyleTracking)
async def dm_style_tracking(
    style_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_DASHBOARD_READERS),
    scope: uuid.UUID | None = Depends(client_scope),
):
    """Style tracking — per-stage quantities for a style, end to end."""
    tracking = await DashboardService(db).dm_style_tracking(
        style_id=style_id, client_scope=scope)
    if tracking is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"No style with id {style_id}.")
    return tracking


@router.get("/direct-manager/pieces/{piece_code}", response_model=PieceTrace)
async def dm_piece_tracking(
    piece_code: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_DASHBOARD_READERS),
):
    """Piece tracking from the DM dashboard — the same shared trace every other
    dashboard serves, through the same helper, so all six URLs stay identical."""
    return await _piece_trace(db, piece_code)
