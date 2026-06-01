"""
================================================================================
modules/production/router.py — Production HTTP API (async)
================================================================================
"""
import uuid
from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.modules.users.deps import get_current_user
from app.modules.clients.service import ClientService
from app.modules.production.service import ProductionService
from app.modules.production import schemas
from app.modules.users.models import User

router = APIRouter(prefix="/production", tags=["Production"])


@router.get("/operations", response_model=list[schemas.OperationRead])
async def list_operations(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await ProductionService(db).list_operations()


@router.post("/events", response_model=schemas.ProductionEventRead, status_code=201)
async def log_event(
    body: schemas.ProductionEventCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return await ProductionService(db).log_event(
        user=user, sku_id=body.sku_id, operation_id=body.operation_id,
        employee_id=body.employee_id, work_date=body.work_date,
        qty=body.qty, bundle_ref=body.bundle_ref,
    )


@router.get("/events", response_model=list[schemas.ProductionEventRead])
async def list_events(
    sku_id: uuid.UUID | None = None,
    employee_id: uuid.UUID | None = None,
    start: date | None = Query(None),
    end: date | None = Query(None),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await ProductionService(db).list_events(
        sku_id=sku_id, employee_id=employee_id, start=start, end=end
    )


@router.get("/styles/{style_id}/progress", response_model=schemas.StyleStageProgress)
async def style_progress(
    style_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    svc = ProductionService(db)
    clients = ClientService(db)
    style = await clients.get_style(style_id)
    stages = await svc.style_progress(style_id)
    skus = await clients.get_skus_for_style(style_id)
    return schemas.StyleStageProgress(
        style_id=style_id,
        style_name=style.name if style else "",
        qty_ordered=sum(s.qty_ordered for s in skus),
        stages=stages,
    )
