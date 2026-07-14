"""
================================================================================
modules/analytics/router.py — Read-only dashboard + drill-down HTTP API (async)
================================================================================
Drill-down: /orders/{id}/tree  ->  /styles/{id}/detail  ->  /pieces/detail
================================================================================
"""
import uuid
from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.enums import UserRole
from app.modules.analytics.service import AnalyticsService
from app.modules.users.deps import get_current_user
from app.modules.users.models import User

router = APIRouter(prefix="/analytics", tags=["Analytics"])


@router.get("/overview")
async def overview(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    return await AnalyticsService(db).factory_overview()


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
    _: User = Depends(get_current_user),
):
    """Order landing view: styles with piece count + current-stage distribution."""
    return await AnalyticsService(db).order_tree(order_id)


@router.get("/styles/{style_id}/detail")
async def style_detail(
    style_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """One style: every piece with its full stage history (employee + date/time)."""
    return await AnalyticsService(db).style_detail(style_id)


@router.get("/pieces/detail")
async def piece_detail(
    piece_code: str | None = Query(None),
    sku_code: str | None = Query(None),
    seq: int | None = Query(None),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """One piece by piece_code OR (sku_code + seq): header + grouped stages."""
    return await AnalyticsService(db).piece_detail(
        piece_code=piece_code, sku_code=sku_code, seq=seq
    )


@router.get("/alerts/stage-spread")
async def stage_spread(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    return await AnalyticsService(db).stage_spread_alerts()


@router.get("/alerts/freight-risk")
async def freight_risk(
    today: date | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await AnalyticsService(db).freight_risk(today)