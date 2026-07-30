"""
================================================================================
modules/drawers/router.py — Drawers & merge-gate HTTP API (async)
================================================================================
POST /drawers/store-scan         scan drawer, then piece → record a part in
POST /drawers/{id}/receive       DM sets RECEIVED → SENDED (the gate release)

store-scan resolves the barcode door (drawer_barcode / piece_barcode) or the
manual door (drawer_id / piece_id) to ids, then delegates. receive is DM/MD only.
================================================================================
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.enums import DrawerPart, UserRole
from app.modules.barcode.service import BarcodeService
from app.modules.drawers import schemas
from app.modules.drawers.service import DrawerService
from app.modules.users.deps import get_current_user, require_roles
from app.modules.users.models import User

router = APIRouter(prefix="/drawers", tags=["Drawers"])


@router.post("/store-scan", response_model=schemas.StoreScanResult)
async def store_scan(
    body: schemas.StoreScanRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Record a leather or lining part arriving in its drawer. Scan the drawer
    first (the merge map is the authority), then the piece."""
    barcodes = BarcodeService(db)
    drawers = DrawerService(db)

    if body.drawer_id:
        drawer_id = body.drawer_id
    elif body.drawer_barcode:
        drawer_id = await barcodes.resolve_drawer_id(body.drawer_barcode)
    else:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Provide drawer_barcode or drawer_id.")

    if body.piece_id:
        piece_id = body.piece_id
    elif body.piece_barcode:
        piece_id = await barcodes.resolve_piece_id(body.piece_barcode)
    else:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Provide piece_barcode or piece_id.")

    return await drawers.store_scan(
        drawer_id=drawer_id, piece_id=piece_id, part=DrawerPart(body.part))


@router.post("/{drawer_id}/receive", response_model=schemas.DrawerTransitionResult)
async def transition(
    drawer_id: uuid.UUID,
    body: schemas.DrawerTransition,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(
        UserRole.DIRECT_MANAGER, UserRole.MANAGING_DIRECTOR)),
):
    """DM/MD sets RECEIVED (completeness confirmed) then SENDED (release to
    line-stitching). No line-stitching event logs for a piece until SENDED."""
    return await DrawerService(db).transition(
        drawer_id, body.transition, actor_id=user.id)