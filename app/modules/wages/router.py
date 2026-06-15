"""
================================================================================
modules/wages/router.py — Wages HTTP API (async). Direct-manager only for writes.
================================================================================
"""
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.enums import UserRole
from app.modules.users.deps import get_current_user, require_roles
from app.modules.wages.service import WageService
from app.modules.wages import schemas
from app.modules.users.models import User

router = APIRouter(prefix="/wages", tags=["Wages"])


@router.post("/rates")
async def set_rate(
    body: schemas.RateSet,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.DIRECT_MANAGER)),
):
    r = await WageService(db).set_rate(
        body.style_id, body.operation_id, body.rate, body.effective_from
    )
    return {"id": str(r.id), "rate": float(r.rate)}


@router.post("/runs", response_model=schemas.WageRunRead)
async def compute_run(
    body: schemas.RunRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.DIRECT_MANAGER)),
):
    return await WageService(db).compute_run(body.period_start, body.period_end)


@router.get("/runs/{run_id}", response_model=schemas.WageRunRead)#extracting the attributes from the ORM object and returning them as a dict that can be serialized to JSON. By defining the model_config with from_attributes=True.
async def get_run(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await WageService(db).get_run(run_id)
