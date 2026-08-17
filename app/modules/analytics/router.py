"""
================================================================================
modules/analytics/router.py — Read-only dashboard + drill-down HTTP API (async)
================================================================================
Drill-down: /orders/{id}/tree  ->  /styles/{id}/detail  ->  /pieces/detail
================================================================================
"""
import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.enums import UserRole
from app.modules.analytics.service import AnalyticsService
from app.modules.users.deps import get_current_user, require_roles
from app.modules.users.models import User

router = APIRouter(prefix="/analytics", tags=["Analytics"])


def client_scope(user: User = Depends(get_current_user)) -> uuid.UUID | None:
    """F33: the client_id a request must be scoped to.

    Returns the caller's own client_id for a CLIENT login (so they can only ever
    see their own orders/styles/pieces), and None for staff roles (who legitimately
    see across clients — e.g. the manager dashboard aggregates). Every analytics
    endpoint reachable by a CLIENT threads this into the service, and a cross-tenant
    id resolves to 404 (existence itself is information).
    """
    return user.client_id if user.role == UserRole.CLIENT else None


# ══════════════════════════════════════════════════════════════════════════════
# REMOVED SCREENS (change-list item 1) — 410 Gone, not deleted routes
# ══════════════════════════════════════════════════════════════════════════════
# The Analytics Overview page and the standalone Risk-Alert page are gone from
# the product. The BOTTLENECK and the ALERTS were the only parts anyone used, and
# they belong on the dashboard every manager already opens — a manager should not
# have to know a separate screen exists to find out their line is blocked. They
# now live at GET /dashboard/alerts, reachable by every manager role.
#
# WHY 410 AND NOT A DELETED ROUTE. A deleted route 404s, and a 404 on a screen
# that worked yesterday reads to a frontend (and to a support call) as "the
# server is broken". 410 Gone says the removal was deliberate and the message
# names the replacement — the same courtesy /production/cutting and
# /production/scan were given when they were retired. Delete these three stubs
# one release after the frontend stops calling them.
_GONE_OVERVIEW = (
    "The Analytics Overview screen has been removed. Factory-wide figures live "
    "on the role dashboards: GET /api/v1/dashboard/direct-manager (whole "
    "factory), or /dashboard/cutting | /lining | /stitching | /store.")
_GONE_ALERTS = (
    "The standalone Risk Alerts screen has been removed. Bottleneck and alerts "
    "are now on GET /api/v1/dashboard/alerts, which every manager role may read.")


@router.get("/overview", deprecated=True)
async def overview(_: User = Depends(get_current_user)):
    """REMOVED (change-list item 1). Always 410. Use the role dashboards."""
    raise HTTPException(status.HTTP_410_GONE, _GONE_OVERVIEW)


@router.get("/explorer")
async def explorer(
    include_pieces: bool = Query(True),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Left-panel nav tree: Client -> Order -> Style -> Piece, scoped to the
    signed-in user. CLIENT-role users see only their own client."""
    client_id = user.client_id if user.role == UserRole.CLIENT else None
    return await AnalyticsService(db).explorer_tree(
        client_id=client_id, include_pieces=include_pieces
    )

@router.get("/orders/{order_id}/tree")
async def order_tree(
    order_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: uuid.UUID | None = Depends(client_scope),
):
    """Order landing view: styles with piece count + current-stage distribution."""
    return await AnalyticsService(db).order_tree(order_id, client_scope=scope)


@router.get("/styles/{style_id}/detail")
async def style_detail(
    style_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: uuid.UUID | None = Depends(client_scope),
):
    """One style: every piece with its full stage history (employee + date/time)."""
    return await AnalyticsService(db).style_detail(style_id, client_scope=scope)

@router.get("/pieces/detail")
async def piece_detail(
    piece_code: str | None = Query(None),
    sku_code: str | None = Query(None),
    seq: int | None = Query(None),
    db: AsyncSession = Depends(get_db),
    scope: uuid.UUID | None = Depends(client_scope),
):
    """One piece by piece_code OR (sku_code + seq): header + grouped stages."""
    return await AnalyticsService(db).piece_detail(
        piece_code=piece_code, sku_code=sku_code, seq=seq, client_scope=scope
    )
    
@router.get("/employee-rates")
async def employee_rate_analytics(
    start: date = Query(...),
    end: date = Query(...),
    employee_id: uuid.UUID | None = Query(None),
    style_code: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(
        UserRole.DIRECT_MANAGER, UserRole.MANAGING_DIRECTOR, UserRole.HR)),
):
    """Per-employee rate / piece / earnings analytics. Cost data — DM, MD, HR only."""
    return await AnalyticsService(db).employee_rate_analytics(
        start=start, end=end, employee_id=employee_id, style_code=style_code)


@router.get("/alerts/stage-spread", deprecated=True)
async def stage_spread(_: User = Depends(get_current_user)):
    """REMOVED (change-list item 1). Always 410. Use GET /dashboard/alerts."""
    raise HTTPException(status.HTTP_410_GONE, _GONE_ALERTS)


@router.get("/alerts/freight-risk", deprecated=True)
async def freight_risk(_: User = Depends(get_current_user)):
    """REMOVED (change-list item 1). Always 410. Use GET /dashboard/alerts."""
    raise HTTPException(status.HTTP_410_GONE, _GONE_ALERTS)



@router.get("/pieces/{piece_code}/story")
async def piece_life_story(
    piece_code: str,
    db: AsyncSession = Depends(get_db),
    scope: uuid.UUID | None = Depends(client_scope),
):
    return await AnalyticsService(db).piece_life_story(piece_code, client_scope=scope)

@router.get("/consumption")
async def consumption(
    order_id: uuid.UUID | None = Query(None),
    style_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    scope: uuid.UUID | None = Depends(client_scope),
):
    return await AnalyticsService(db).consumption_vs_stock(
        order_id=order_id, style_id=style_id, client_scope=scope)