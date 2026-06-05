"""
================================================================================
modules/analytics/router.py — Read-only dashboard + alerts HTTP API (async)
================================================================================
"""
from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.modules.users.deps import get_current_user
from app.modules.analytics.service import AnalyticsService
from app.modules.users.models import User

router = APIRouter(prefix="/analytics", tags=["Analytics"])


@router.get("/overview")
async def overview(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)):
    return await AnalyticsService(db).factory_overview()


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