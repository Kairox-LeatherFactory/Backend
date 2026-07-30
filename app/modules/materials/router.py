"""
================================================================================
modules/materials/router.py — Materials, inventory & supplier HTTP API (async)
================================================================================
POST /materials/lots           create a lot (+ child barcode + stock)
GET  /materials/stock          on-hand / reserved / available + shortfall
POST /materials/receive        approved / rejected receiving
POST /suppliers/orders         raise an order on a shortfall (ORDERED)
PATCH /suppliers/orders/{id}   ORDERED → ARRIVED

All cost/stock data — DM, MD (+ HR read on stock). Cutting managers may create
lots (they do it when material arrives) and check stock.
================================================================================
"""
import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.enums import UserRole
from app.modules.materials import schemas
from app.modules.materials.service import MaterialService
from app.modules.users.deps import require_roles
from app.modules.users.models import User

router = APIRouter(prefix="/materials", tags=["Materials"])
sup_router = APIRouter(prefix="/suppliers", tags=["Suppliers"])

_LOT_WRITERS = require_roles(
    UserRole.DIRECT_MANAGER, UserRole.MANAGING_DIRECTOR, UserRole.CUTTING_MANAGER)
_STOCK_READERS = require_roles(
    UserRole.DIRECT_MANAGER, UserRole.MANAGING_DIRECTOR, UserRole.HR,
    UserRole.CUTTING_MANAGER, UserRole.STITCHING_MANAGER)
_DM = require_roles(UserRole.DIRECT_MANAGER, UserRole.MANAGING_DIRECTOR)


@router.post("/lots", response_model=schemas.LotCreateResult, status_code=201)
async def create_lot(
    body: schemas.LotCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_LOT_WRITERS),
):
    """Create a material lot. Registers a child barcode and adds the quantity to
    stock in one transaction."""
    return await MaterialService(db).create_lot(body)


@router.get("/spec")
async def material_spec(
    category: str = Query(..., description="LEATHER | LINING | ACCESSORY"),
    subtype: str | None = Query(None, description="RIBS/KNIT ; BUTTON/ZIP/THREAD/OTHER"),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_STOCK_READERS),
):
    """The fields to SHOW for a category: which to filter stock by, which are
    required to add a lot, and the quantity field + unit. Drives the Add-New form
    and the stock search boxes so the UI matches the material class exactly."""
    return MaterialService(db).filter_fields(category, subtype)


@router.get("/stock", response_model=schemas.StockRead)
async def stock(
    category: str | None = Query(None),
    subtype: str | None = Query(None),
    article: str | None = Query(None),
    colour: str | None = Query(None),
    thickness: str | None = Query(None),
    size: str | None = Query(None),
    required: float | None = Query(None, description="Compute short_by against this."),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_STOCK_READERS),
):
    """On-hand / reserved / available for a filtered material, with shortfall +
    a suggested supplier when short."""
    return await MaterialService(db).stock(
        category=category, subtype=subtype, article=article, colour=colour,
        thickness=thickness, size=size, required=required)


@router.post("/receive", response_model=schemas.ReceiveResult)
async def receive(
    body: schemas.ReceiveRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_DM),
):
    """Record receiving: approved adds to stock, the requirement is reserved,
    rejected is logged for supplier quality history."""
    return await MaterialService(db).receive(body, actor_id=user.id)


@sup_router.post("/orders", response_model=schemas.SupplierOrderResult, status_code=201)
async def create_order(
    body: schemas.SupplierOrderCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_DM),
):
    """Raise a manual supplier order (status ORDERED). Suggests a supplier from
    the article when none is given."""
    return await MaterialService(db).create_order(body, actor_id=user.id)


@sup_router.patch("/orders/{order_id}", response_model=schemas.SupplierOrderPatchResult)
async def mark_arrived(
    order_id: uuid.UUID,
    body: schemas.SupplierOrderPatch,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_DM),
):
    """Flip ORDERED → ARRIVED. Cues the receiving screen. Idempotent."""
    return await MaterialService(db).mark_arrived(order_id)