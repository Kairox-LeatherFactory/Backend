"""
================================================================================
modules/barcode/router.py — Barcode registry HTTP API (async)
================================================================================
GET  /barcode/resolve                  every scan's front door
POST /barcode/print                    printable payload for a set of codes
PATCH /employees/{id}/barcode          reissue / deactivate (lives here, employee
                                       barcodes are a barcode concern)

resolve is available to any authenticated staff INCLUDING the attendance path —
a worker scanning their own card to check in must resolve it. So resolve is NOT
behind block_employees; the write endpoints are.
================================================================================
"""
import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.enums import UserRole
from app.modules.barcode import schemas
from app.modules.barcode.service import BarcodeService
from app.modules.users.deps import get_current_user, require_roles
from app.modules.users.models import User

router = APIRouter(prefix="/barcode", tags=["Barcode"])
# employee-barcode lifecycle mounts under /employees but is a barcode concern.
emp_router = APIRouter(prefix="/employees", tags=["Barcode"])


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