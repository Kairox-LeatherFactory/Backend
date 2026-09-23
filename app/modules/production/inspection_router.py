"""
================================================================================
production/inspection_router.py — HTTP only. Reject, decide, and who is to blame.
================================================================================
POST  /inspections                  record a PASS, or raise a REJECT
POST  /inspections/{id}/approve     DM: send the garment back
POST  /inspections/{id}/decline     DM: no, it carries on
GET   /inspections                  what is waiting to be decided
GET   /inspections/responsibility   defects by the worker answerable for them
GET   /inspections/pieces/{code}    one garment's inspection history

WHO. Anyone who stands at a stage can SEE a defect, so every manager and HR may
RAISE one. Only the DM (and the MD, who outranks everyone) may send a garment
backwards — that re-opens a completed stage, re-orders the line's work and can
cost material.
================================================================================
"""
import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.enums import UserRole
from app.modules.barcode.service import BarcodeService
from app.modules.production.inspection import InspectionService
from app.modules.production.inspection_schemas import (
    InspectionDecision, InspectionRead, InspectionRequest, ResponsibilityRow,
)
from app.modules.users.deps import require_roles
from app.modules.users.models import User

router = APIRouter(prefix="/inspections", tags=["inspections"])

_RAISERS = require_roles(
    UserRole.MANAGING_DIRECTOR, UserRole.DIRECT_MANAGER, UserRole.HR,
    UserRole.CUTTING_MANAGER, UserRole.LINING_MANAGER,
    UserRole.STITCHING_MANAGER, UserRole.STORE_MANAGER, UserRole.SUPERVISOR)
# SENDING A GARMENT BACKWARDS IS THE DM'S CALL, and deliberately no wider.
_APPROVERS = require_roles(UserRole.DIRECT_MANAGER, UserRole.MANAGING_DIRECTOR)


@router.post("", response_model=InspectionRead,
             status_code=status.HTTP_201_CREATED)
async def raise_inspection(
    body: InspectionRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_RAISERS),
):
    """Record a PASS, or raise a REJECT for the DM.

    A PASS closes immediately — there is nothing to approve about a garment that
    is fine, and making somebody sign one off means nobody records passes at all.
    """
    piece_id = body.piece_id
    if piece_id is None:
        piece_id = await BarcodeService(db).resolve_piece_id(body.piece_barcode)
    return await InspectionService(db).raise_inspection(
        piece_id=piece_id, found_at_stage=body.found_at_stage,
        verdict=body.verdict, action=body.action,
        return_to_stage=body.return_to_stage, defect_type=body.defect_type,
        responsible_employee_id=body.responsible_employee_id,
        responsible_stage=body.responsible_stage, reason=body.reason,
        # The LOGIN signs it. The WORKER blamed is responsible_employee_id, and
        # they are different people in different tables.
        actor_user_id=user.id, actor_name=user.name)


@router.post("/{inspection_id}/approve", response_model=InspectionRead)
async def approve(
    inspection_id: uuid.UUID,
    body: InspectionDecision | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_APPROVERS),
):
    """The DM agrees: the garment goes back (or is fixed where it stands)."""
    return await InspectionService(db).decide(
        inspection_id, approve=True, actor_user_id=user.id,
        actor_name=user.name, note=(body.note if body else None))


@router.post("/{inspection_id}/decline", response_model=InspectionRead)
async def decline(
    inspection_id: uuid.UUID,
    body: InspectionDecision | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_APPROVERS),
):
    """The DM disagrees: the garment stays where it is and carries on."""
    return await InspectionService(db).decide(
        inspection_id, approve=False, actor_user_id=user.id,
        actor_name=user.name, note=(body.note if body else None))


@router.get("", response_model=list[InspectionRead])
async def list_inspections(
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=200, le=1000),
    offset: int = Query(default=0, ge=0,
                        description="Rows to skip before returning `limit` rows."),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_RAISERS),
):
    """Open rejections by default — the DM's queue.

    `offset` is here because a `limit` on its own is a CAP, not a pager: without
    it a caller can ask for the first 200 rows and has no way to ask for the
    next 200.
    """
    return await InspectionService(db).list_open(status_filter=status_filter,
                                                 limit=limit, offset=offset)


@router.get("/responsibility", response_model=list[ResponsibilityRow])
async def responsibility(
    employee_id: uuid.UUID | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_APPROVERS),
):
    """Defects by the worker answerable for them.

    WORKMANSHIP ONLY. Product damage names nobody, so counting it here would put
    a supplier's bad hide onto a person's record.

    Declared BEFORE /pieces/{code} so the literal segment wins the match — FastAPI
    resolves in declaration order, and the other route would otherwise swallow
    "responsibility" as a piece code.
    """
    return await InspectionService(db).responsibility_report(
        employee_id=employee_id)


@router.get("/pieces/{piece_code}", response_model=list[InspectionRead])
async def for_piece(
    piece_code: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_RAISERS),
):
    """One garment's inspection history, oldest first."""
    piece_id = await BarcodeService(db).resolve_piece_id(piece_code)
    return await InspectionService(db).for_piece(piece_id)
