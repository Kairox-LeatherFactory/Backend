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

from fastapi import APIRouter, Depends, HTTPException, Query, status
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
    offset: int = Query(default=0, ge=0,
                        description="Rows to skip before returning `limit` rows."),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_READERS),
):
    """What is in the store right now, one page at a time.

    `offset` and `total` are what make this pageable: a `limit` on its own is a
    cap, and a busy store holds far more than one page of garments.
    """
    return await StoreService(db).list_pieces(state=state, style_id=style_id,
                                              limit=limit, offset=offset)


@router.get("/pieces/{piece_code}/materials",
            response_model=schemas.PieceMaterials)
async def piece_materials(
    piece_code: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_READERS),
):
    """WHAT IS MERGED INTO THIS GARMENT — the whole picture, in one read.

    `applies`        the recipe for THIS colourway at THIS size — leather,
                     lining, and every accessory line with `issued_qty` and
                     `outstanding`.
    `not_applicable` the style's OTHER lines, each saying why it is not this
                     garment's: `other_sku` (scoped to another colourway),
                     `other_size` (an L zip is not an S zip), or `zeroed` (this
                     colourway declares it takes none).
    `issued`         the ledger — what physically went in, from which lot, when
                     and through whose card, MANUAL corrections included.
    `consumed`       the leather actually recorded at the cut. It lives on the
                     production event, not the issue ledger, and is merged here
                     so a screen never has to know there were two writes.

    `not_applicable` is the field to read when a kit scan says there is nothing
    to issue while `/material-spec/requirement` shows a full recipe: that view
    is STYLE-WIDE and a kit is issued PER GARMENT.
    """
    svc = StoreService(db)
    piece_id = await BarcodeService(db).resolve_piece_id(piece_code)
    piece = await svc.get_piece(piece_id)
    if piece is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Piece not found.")
    return await svc.piece_materials(piece)


@router.get("/pieces/{piece_code}", response_model=schemas.StorePieceDetail)
async def find_piece(
    piece_code: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_READERS),
):
    """WHERE IS THIS GARMENT, and what is it still owed — bug #15.

    Under the drawer flow this meant looking up which numbered box held it. There
    is no box: the garment's own record says what it is holding and what it is
    waiting for.

    IT CARRIES THE ACCESSORY CHECKLIST LINE BY LINE. `accessories_in` is a
    roll-up over every declared line, so on its own it cannot tell an operator
    whether the zip is missing or the buttons are. `accessories[]` names each
    line with what has been issued against it and what is still owed — which is
    how you verify a kit without issuing anything.
    """
    svc = StoreService(db)
    piece_id = await BarcodeService(db).resolve_piece_id(piece_code)
    piece = await svc.get_piece(piece_id)
    return await svc.piece_detail(piece)
