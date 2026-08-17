"""
================================================================================
modules/barcode/router.py — Barcode registry HTTP API (async)
================================================================================
GET  /barcode/resolve                  every scan's front door
POST /barcode/print                    printable payload for a set of codes
PATCH /employees/{id}/barcode          reissue / deactivate (lives here, employee
                                       barcodes are a barcode concern)

resolve is available to any authenticated staff INCLUDING the attendance path —
the gate operator (SECURITY / HR / MD / DM) resolves a worker's card before
checking them in. Workers themselves hold no login. resolve stays off
block_employees so a legacy employee token cannot 403 the attendance screen; the
write endpoints are locked.
================================================================================
"""
from datetime import datetime
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from app.core.database import get_db
from app.core.enums import UserRole
from app.modules.barcode import schemas
from app.modules.barcode.service import BarcodeService
from app.modules.users.deps import get_current_user, require_roles
from app.modules.users.models import User

router = APIRouter(prefix="/barcode", tags=["Barcode"])
# employee-barcode lifecycle mounts under /employees but is a barcode concern.
emp_router = APIRouter(prefix="/employees", tags=["Barcode"])

_SCREEN_READERS = require_roles(
    UserRole.MANAGING_DIRECTOR, UserRole.DIRECT_MANAGER, UserRole.HR,
    UserRole.SUPERVISOR, UserRole.CUTTING_MANAGER, UserRole.LINING_MANAGER,
    UserRole.STITCHING_MANAGER,
)

@router.get("/resolve", response_model=schemas.BarcodeResolve)
async def resolve(
    code: str = Query(..., description="The scanned/typed barcode string."),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),   # NOT block_employees — attendance uses it
):
    """Resolve any barcode to its type + live payload. 404 unknown, 410 retired."""
    return await BarcodeService(db).resolve(code)


@router.post("/print", response_model=schemas.PrintResponse)
async def print_labels(
    body: schemas.PrintRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(
        UserRole.DIRECT_MANAGER, UserRole.MANAGING_DIRECTOR,
        UserRole.CUTTING_MANAGER, UserRole.STITCHING_MANAGER, UserRole.HR)),
):
    """Code128-ready payload for one or many codes (or all pieces of a SKU/order)."""
    return await BarcodeService(db).print_payload(
        codes=body.codes, sku_id=body.sku_id, order_id=body.order_id)


@emp_router.patch("/{employee_id}/barcode", response_model=schemas.BarcodeActionResult)
async def employee_barcode_action(
    employee_id: uuid.UUID,
    body: schemas.BarcodeAction,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(
        UserRole.DIRECT_MANAGER, UserRole.MANAGING_DIRECTOR, UserRole.HR)),
):
    """Reissue a card (damaged/lost) or deactivate it (worker left). Deactivation
    NEVER deletes the employee or their history — it retires the scannable code."""
    svc = BarcodeService(db)
    if body.action == "reissue":
        return await svc.reissue_employee_barcode(employee_id, actor_id=user.id)
    return await svc.deactivate_employee_barcode(employee_id, actor_id=user.id)


@router.get("/materials")
async def material_barcodes(
    category: str | None = Query(None, description="LEATHER | LINING | ACCESSORY"),
    active_only: bool = Query(True, description="Hide retired lot labels."),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_SCREEN_READERS),
):
    """THE MATERIAL-BARCODE SCREEN (change-list item 7).

    The barcode section had screens for pieces, drawers and employee cards but
    none for material lots — so a lot minted a code at creation and nobody could
    reprint it once the first label was lost or damaged.

    Each row carries `code` (encode as Code128) and `label_line` (typeset
    underneath as text), the same convention as POST /barcode/print. A row with
    `status: retired` belongs to a retired lot: show it greyed, do not print it.

    NOTE FOR THE UI: the drawer screen's label reads "bucket barcode" and should
    read "drawer barcode". The backend has only ever called it DRAWER — there is
    no `bucket` anywhere in the API — so that rename is frontend-only."""
    return await BarcodeService(db).list_lot_barcodes(
        category=category, active_only=active_only)


@router.get("/orders", response_model=list[schemas.OrderPickerRow])
async def list_barcode_orders(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_SCREEN_READERS),
):
    """Orders that have generated barcodes — the picker. Unique per order_number.
    Shows minted count + generated date range so the user picks the right order."""
    return await BarcodeService(db).list_orders()
 
@router.get("/orders/{order_id}/skus", response_model=list[schemas.OrderSkuOption])
async def list_order_sku_options(
    order_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_SCREEN_READERS),
):
    """SKU + style options to populate the filter dropdowns for this order."""
    return await BarcodeService(db).list_order_skus(order_id)
 
 
@router.get("/orders/{order_id}/analytics", response_model=schemas.OrderAnalytics)
async def order_barcode_analytics(
    order_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_SCREEN_READERS),
):
    """Per-order: planned vs generated vs balance (order total AND per style).
    Includes duplicates=0 integrity proof and a half_minted flag."""
    return await BarcodeService(db).order_analytics(order_id)
 
 
@router.get("/orders/{order_id}/barcodes", response_model=schemas.BarcodeHistoryPage)
async def order_barcode_history(
    order_id: uuid.UUID,
    sku_id: uuid.UUID | None = Query(None, description="Filter to one SKU."),
    style_id: uuid.UUID | None = Query(None, description="Filter to one style."),
    size: str | None = Query(None, description="Filter to one size (style+size)."),
    status: str | None = Query(None, pattern="^(active|retired)$"),
    date_from: datetime | None = Query(None, description="Generated at/after."),
    date_to: datetime | None = Query(None, description="Generated at/before."),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """The history table: order → style → piece, filterable by SKU, style, or
    style+size, plus status and generated-date range. Paginated (JP ~= 1,273)."""
    return await BarcodeService(db).list_history(
        order_id,sku_id=sku_id, style_id=style_id, size=size,
        status_filter=status, date_from=date_from, date_to=date_to,
        page=page, page_size=page_size,
    )
 
 
@router.get("/detail", response_model=schemas.BarcodeResolve)
async def barcode_detail(
    code: str = Query(..., description="The clicked/scanned barcode string."),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_SCREEN_READERS),
):
    """Full detail for one barcode (click-through from the history list). Same
    rich payload as /resolve, tenancy-scoped for CLIENT logins."""
    return await BarcodeService(db).barcode_detail(code)

@router.get("/orders/by-number/{order_number}", response_model=schemas.OrderPickerRow)
async def resolve_order_by_number(
    order_number: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_SCREEN_READERS),
):
    """Resolve a human order_number to its picker row (id + minted counts)."""
    svc = BarcodeService(db)
    oid = await svc.resolve_order_id(order_number)
    rows = await svc.list_orders()
    for r in rows:
        if r["order_id"] == oid:
            return r
    raise HTTPException(status.HTTP_404_NOT_FOUND, "Order has no barcodes yet.")