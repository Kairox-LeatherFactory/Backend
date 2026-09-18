"""
================================================================================
modules/cutting/router.py — HTTP only. The grid that replaced the Excel.
================================================================================
GET    /cutting/grid                       the sheet for one style + colour
POST   /cutting/rows/generate              one row per un-cut garment, hides on
PATCH  /cutting/rows/{id}                  edit any cell
POST   /cutting/rows/{id}/sheets           the cutter needed another hide
DELETE /cutting/rows/{id}/sheets/{sid}     he handed one back
PATCH  /cutting/rows/{id}/sheets/{sid}     correct a hide's measurement
POST   /cutting/rows/{id}/approve          freeze it — audited
POST   /cutting/rows/{id}/reopen           un-freeze it — also audited

WHO. The cutting manager owns this screen; DM and MD are on it because they are
on everything (the two bypass roles). The LINING manager is deliberately absent:
lining is cut by the metre and has no hides to track.
================================================================================
"""
import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.enums import UserRole
from app.modules.cutting import schemas
from app.modules.cutting.service import CuttingService
from app.modules.users.deps import require_roles
from app.modules.users.models import User

router = APIRouter(prefix="/cutting", tags=["cutting"])

_CUTTING = require_roles(
    UserRole.MANAGING_DIRECTOR, UserRole.DIRECT_MANAGER, UserRole.CUTTING_MANAGER)


@router.get("/grid", response_model=schemas.CuttingGrid)
async def grid(
    style_id: uuid.UUID,
    colour: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_CUTTING),
):
    """The cutting sheet for one style + colour, as the Excel laid it out.

    Also returns who is checked in today, because production refuses to log an
    absent worker — a grid that let one be assigned would collect the work and
    then fail at the scan, after the leather was already handed out.
    """
    return await CuttingService(db).grid(style_id=style_id, colour=colour)


@router.post("/rows/generate", response_model=schemas.GenerateResult,
             status_code=status.HTTP_201_CREATED)
async def generate(
    body: schemas.GenerateRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_CUTTING),
):
    """One DRAFT row per un-cut garment, with hides already allocated to it.

    Safe to press twice: rows are only created for pieces that have none.
    """
    return await CuttingService(db).generate(body, entered_by=user.name)


@router.patch("/rows/{row_id}", response_model=schemas.RowRead)
async def patch_row(
    row_id: uuid.UUID,
    body: schemas.RowPatch,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_CUTTING),
):
    return await CuttingService(db).patch_row(row_id, body)


@router.post("/rows/{row_id}/sheets", response_model=schemas.RowRead)
async def add_sheet(
    row_id: uuid.UUID,
    body: schemas.SheetAdd,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_CUTTING),
):
    """The cutter needed one more skin. Scan it, pick it, or create it."""
    return await CuttingService(db).add_sheet(row_id, body)


@router.delete("/rows/{row_id}/sheets/{sheet_id}", response_model=schemas.RowRead)
async def remove_sheet(
    row_id: uuid.UUID,
    sheet_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_CUTTING),
):
    """He handed one back. It returns to the shelf, available to the next row."""
    return await CuttingService(db).remove_sheet(row_id, sheet_id)


@router.patch("/rows/{row_id}/sheets/{sheet_id}", response_model=schemas.RowRead)
async def patch_sheet(
    row_id: uuid.UUID,
    sheet_id: uuid.UUID,
    body: schemas.SheetPatch,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_CUTTING),
):
    """Correct what a hide measures. It follows the skin, not the row."""
    return await CuttingService(db).patch_sheet(row_id, sheet_id, body)


@router.post("/rows/{row_id}/approve", response_model=schemas.ApproveResult)
async def approve(
    row_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_CUTTING),
):
    """Freeze the row: these hides, this total, this cutter.

    `user.id` is an app_user id — the LOGIN — which is what AuditLog.actor_user_id
    requires. The WORKER is the row's cutter_employee_id and is a different
    person in a different table.
    """
    return await CuttingService(db).approve(
        row_id, actor_user_id=user.id, actor_name=user.name)


@router.post("/rows/{row_id}/reopen", response_model=schemas.RowRead)
async def reopen(
    row_id: uuid.UUID,
    reason: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_CUTTING),
):
    """Un-freeze an approved row. Recorded, because it un-signs a signature."""
    return await CuttingService(db).reopen(
        row_id, actor_user_id=user.id, reason=reason, actor_name=user.name)
