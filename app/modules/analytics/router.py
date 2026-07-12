"""
================================================================================
modules/analytics/router.py — Read-only dashboard + alerts HTTP API (async)
================================================================================
"""
import uuid
from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.modules.analytics.service import AnalyticsService
from app.modules.users.deps import get_current_user
from app.modules.users.models import User

router = APIRouter(prefix="/analytics", tags=["Analytics"])


@router.get("/overview")
async def overview(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    return await AnalyticsService(db).factory_overview()


@router.get("/production-feed")
async def production_feed(
    piece_code: str | None = Query(None),
    employee_id: uuid.UUID | None = Query(None),
    operation_id: uuid.UUID | None = Query(None),
    style_id: uuid.UUID | None = Query(None),
    order_id: uuid.UUID | None = Query(None),
    start: date | None = Query(None),
    end: date | None = Query(None),
    limit: int = Query(500, le=2000),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Employee · SKU name · bundle id · stage feed, filterable every which way."""
    return await AnalyticsService(db).production_feed(
        piece_code=piece_code, employee_id=employee_id, operation_id=operation_id,
        style_id=style_id, order_id=order_id, start=start, end=end, limit=limit,
    )


@router.get("/pieces/{code}/history")
async def piece_history(
    code: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """One piece's full stage-by-stage traveler."""
    return await AnalyticsService(db).piece_history(code)


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