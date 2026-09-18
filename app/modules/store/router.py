"""
================================================================================
modules/store/router.py — HTTP only. Two scans, not three.
================================================================================
POST /store/scan                  worker + garment → part into the store
POST /store/send                  release garments to line-stitching
GET  /store/pieces                what is in the store right now
GET  /store/pieces/{piece_code}   WHERE IS THIS GARMENT — the lookup page

THE LOOKUP IS DELIBERATELY WIDE (bug #15). The DM assigns somebody to place
garments in the store, and that person has no DM login — so under the old routes
they had no way to look a barcode up at all. Reading where a garment is tells
nobody anything they should not know, and refusing it just sends them to find the
DM. Writing is still narrow.
================================================================================
"""
import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.enums import UserRole
from app.modules.barcode.service import BarcodeService
from app.modules.store import schemas
from app.modules.store.service import StoreService
from app.modules.users.deps import require_roles
from app.modules.users.models import User

router = APIRouter(prefix="/store", tags=["store"])

# WHO MAY RECEIVE INTO THE STORE. Narrower than the old drawer screen, which let
# every floor role scan. The store is where the garment is assembled and where
# stock is spent on it, so the login is one of the three roles that actually
# stand there — store manager, stitching manager, DM (MD bypasses everything).
# A cutting or lining manager hands the part OVER; they do not receive it.
#
# The WORKER is still scanned separately and recorded on the movement: the login
# says who is accountable, the card says whose hands the part passed through.
_FLOOR = require_roles(
    UserRole.MANAGING_DIRECTOR, UserRole.DIRECT_MANAGER,
    UserRole.STITCHING_MANAGER, UserRole.STORE_MANAGER)
_SENDERS = require_roles(
    UserRole.MANAGING_DIRECTOR, UserRole.DIRECT_MANAGER,
    UserRole.STORE_MANAGER, UserRole.STITCHING_MANAGER)
_READERS = require_roles(
    UserRole.MANAGING_DIRECTOR, UserRole.DIRECT_MANAGER,
    UserRole.CUTTING_MANAGER, UserRole.LINING_MANAGER,
    UserRole.STITCHING_MANAGER, UserRole.SUPERVISOR, UserRole.STORE_MANAGER,
    UserRole.HR)


@router.post("/scan", response_model=schemas.StoreScanResult,
             status_code=status.HTTP_201_CREATED)
async def store_scan(
    body: schemas.StoreScanRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_FLOOR),
):
    """Scan the worker, scan the garment. The drawer scan is gone.

    Barcodes are resolved to ids HERE so the service sees ids only
    (CLAUDE.md §15) — the same split the production log uses.
    """
    barcodes = BarcodeService(db)
    employee_id = await barcodes.resolve_actor(
        employee_barcode=body.employee_barcode, employee_id=body.employee_id)
    piece_id = body.piece_id
    if piece_id is None:
        piece_id = await barcodes.resolve_piece_id(body.piece_barcode)

    return await StoreService(db).store_scan(
        piece_id=piece_id, employee_id=employee_id, part=body.part,
        lines=body.lines,
        # The LOGIN signs the audit row; the WORKER is employee_id above.
        actor_user_id=user.id, entered_by=user.name)


@router.post("/send", response_model=schemas.StoreSendResult)
async def send(
    body: schemas.StoreSendRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_SENDERS),
):
    """Release complete garments. One incomplete never loses the complete ones."""
    barcodes = BarcodeService(db)
    piece_ids = list(body.piece_ids)
    for code in body.piece_barcodes:
        piece_ids.append(await barcodes.resolve_piece_id(code))
    return await StoreService(db).send(
        piece_ids=piece_ids, actor_user_id=user.id, actor_name=user.name)


@router.get("/pieces", response_model=schemas.StoreList)
async def list_pieces(
    state: str | None = Query(default=None,
                              description="waiting|merged|holding_leather|"
                                          "holding_lining|holding_both|"
                                          "received|sended"),
    style_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=200, le=1000),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_READERS),
):
    return await StoreService(db).list_pieces(state=state, style_id=style_id,
                                              limit=limit)


@router.get("/pieces/{piece_code}", response_model=schemas.StorePieceRow)
async def find_piece(
    piece_code: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_READERS),
):
    """WHERE IS THIS GARMENT — bug #15, and now a one-line answer.

    Under the drawer flow this meant looking up which numbered box held it. There
    is no box: the garment's own record says what it is holding and what it is
    waiting for.
    """
    svc = StoreService(db)
    piece_id = await BarcodeService(db).resolve_piece_id(piece_code)
    piece = await svc.get_piece(piece_id)
    return await svc.piece_row(piece)
