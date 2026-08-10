"""
================================================================================
modules/drawers/router.py — Drawers & merge-gate HTTP API (async)
================================================================================
GET  /drawers                    the label sheet: every drawer + its barcode
POST /drawers/store-scan         scan drawer, then piece → record a part in
POST /drawers/{id}/receive       DM sets RECEIVED → SENDED (the gate release)

store-scan resolves the barcode door (drawer_barcode / piece_barcode) or the
manual door (drawer_id / piece_id) to ids, then delegates. receive is DM/MD only.
================================================================================
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.enums import DrawerPart, DrawerState, UserRole
from app.modules.barcode.service import BarcodeService
from app.modules.drawers import schemas
from app.modules.drawers.service import DrawerService
from app.modules.users.deps import get_current_user, require_roles
from app.modules.users.models import User

router = APIRouter(prefix="/drawers", tags=["Drawers"])

_FLOOR = require_roles(
    UserRole.MANAGING_DIRECTOR, UserRole.DIRECT_MANAGER,
    UserRole.CUTTING_MANAGER, UserRole.LINING_MANAGER,
    UserRole.STITCHING_MANAGER, UserRole.SUPERVISOR)


@router.get("", response_model=schemas.DrawerLabelPage)
async def list_drawers(
    state: str | None = Query(None, description="Filter by DrawerState, e.g. waiting"),
    seq_from: int | None = Query(None, ge=1, description="Print a range: first seq."),
    seq_to: int | None = Query(None, ge=1, description="Print a range: last seq."),
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_FLOOR),
):
    """Every drawer with its scannable DRAWER barcode — the print sheet.

    Ordered by `seq`, so DRW-0001…DRW-0200 come out in drawer order. `limit`
    defaults to 500 (a 200-drawer pool prints in one call); page with
    `offset` beyond that. `total` counts the filter, not the page.

    Encode `barcode` on the label. A row with `barcode: null` is a drawer with no
    registry code — it cannot be scanned, so print nothing for it and re-run
    `python -m scripts.gen_drawer_barcodes` to mint the missing code."""
    if state is not None:
        valid = {s.value for s in DrawerState}
        if state not in valid:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"state must be one of {sorted(valid)}.")
    if seq_from is not None and seq_to is not None and seq_from > seq_to:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "seq_from must not exceed seq_to.")
    return await DrawerService(db).list_labels(
        state=state, seq_from=seq_from, seq_to=seq_to, limit=limit, offset=offset)


@router.post("/store-scan", response_model=schemas.StoreScanResult)
async def store_scan(
    body: schemas.StoreScanRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_FLOOR),
):
    """Record a leather or lining part arriving in its drawer. Scan the drawer
    first (the merge map is the authority), then the piece.

    H2: floor staff only. This write feeds the merge gate that releases a piece
    into line-stitching — CLIENT and VIEWER tokens must not reach it."""
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