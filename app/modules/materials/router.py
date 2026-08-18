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
sup_router = APIRouter(prefix="/suppliers", tags=["MaterialSuppliers"])

_LOT_WRITERS = require_roles(
    UserRole.DIRECT_MANAGER, UserRole.MANAGING_DIRECTOR, UserRole.CUTTING_MANAGER,UserRole.LINING_MANAGER)
_STOCK_READERS = require_roles(
    UserRole.DIRECT_MANAGER, UserRole.MANAGING_DIRECTOR, UserRole.HR,
    UserRole.CUTTING_MANAGER, UserRole.STITCHING_MANAGER, UserRole.LINING_MANAGER, UserRole.SECURITY, UserRole.STORE_MANAGER)
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


@router.get("/lots", response_model=schemas.LotListResult)
async def list_lots(
    category: str | None = Query(None, description="LEATHER | LINING | ACCESSORY"),
    subtype: str | None = Query(None),
    article: str | None = Query(None),
    colour: str | None = Query(None),
    thickness: str | None = Query(None),
    size: str | None = Query(None),
    sku_id: uuid.UUID | None = Query(
        None, description="Pre-select the lot this SKU was last cut from."),
    required: float | None = Query(
        None, description="Total qty this cut needs; flags lots that cover it."),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_STOCK_READERS),
):
    """THE LOT PICKER for the cut screen — filter down to the lot, get its id.

    This is the endpoint that lets a frontend supply `leather_lot_id` /
    `lining_lot_id` to `POST /production/log`. The other two material GETs
    cannot: `/materials/spec` returns the FORM DEFINITION (which boxes to
    render), and `/materials/stock` returns AGGREGATE TOTALS with the lot ids
    summed away.

    Typical cut-screen flow:
        GET /materials/lots?category=LEATHER&sku_id=<sku>
          → `options` fills the article / colour / thickness dropdowns
          → the row with `last_used_for_sku: true` is pre-selected
        …narrow with &article=&colour=&thickness= as the user picks
          → one row (material is one lot per spec), take its `lot_id`
        POST /production/log with consumption.leather_lot_id + dcm

    Pass `required` (dcm per piece × piece count) and each lot reports
    `covers_required`, so a lot that cannot cover the batch can be greyed out
    BEFORE the cut instead of warning after it.

    Exhausted lots are returned, not hidden — a manager searching for a lot they
    know exists must find it, with `available: 0` explaining itself."""
    return await MaterialService(db).list_lots(
        category=category, subtype=subtype, article=article, colour=colour,
        thickness=thickness, size=size, sku_id=sku_id, required=required)


# ══════════════════════════════════════════════════════════════════════════════
# LOT CRUD (change-list item 7) — the stock-management screen
# ══════════════════════════════════════════════════════════════════════════════
# Declared BEFORE /lots/{lot_id} would matter if any static segment followed
# /lots; it does not, so ordering here is only readability.
@router.get("/lots/{lot_id}", response_model=schemas.LotDetail)
async def get_lot(
    lot_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_STOCK_READERS),
):
    """One lot: identity, its three stock numbers, its barcode, and which of its
    fields the edit form may show."""
    return await MaterialService(db).get_lot(lot_id)


@router.patch("/lots/{lot_id}", response_model=schemas.LotDetail)
async def update_lot(
    lot_id: uuid.UUID,
    body: schemas.LotPatch,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_LOT_WRITERS),
):
    """Correct a lot's IDENTITY — article, colour, thickness, size, supplier.

    CATEGORY, SUBTYPE, UOM AND on_hand ARE NOT PATCHABLE, on purpose:
      • category/subtype decide the UOM and the whole required-field set. Moving
        LEATHER→LINING would leave a lot measured in dcm claiming to be metres,
        and every cut event already pointing at it would silently re-denominate.
      • on_hand is a LEDGER. It moves by receiving and by cutting. Setting it
        directly would make the stock and the movement history disagree with no
        record of who changed it — use PATCH /lots/{id}/adjust, which is the same
        change with a reason attached.

    409 if the new spec collides with another lot: material is one lot per spec,
    or the picker shows two rows a cutter cannot tell apart."""
    return await MaterialService(db).update_lot(
        lot_id, body.model_dump(exclude_unset=True))


@router.patch("/lots/{lot_id}/adjust", response_model=schemas.LotDetail)
async def adjust_lot(
    lot_id: uuid.UUID,
    body: schemas.LotAdjust,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_DM),
):
    """A COUNTED STOCK CORRECTION: +/- delta, with a reason, audited.

    The honest form of "edit the quantity" — it records a MOVEMENT rather than
    overwriting the number, so the ledger still adds up and someone can ask a
    year later why 40 dcm disappeared. Refuses to take stock below what is
    already reserved for a cut, and refuses to go negative."""
    return await MaterialService(db).adjust_lot(
        lot_id, delta=body.delta, reason=body.reason, actor_id=user.id)


@router.delete("/lots/{lot_id}", response_model=schemas.LotRetireResult)
async def retire_lot(
    lot_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_DM),
):
    """RETIRE a lot — deactivate it and retire its barcode. Never a hard delete.

    Present it as "retire", not "delete". Cut events point at this lot and are
    the consumption history the costing and traceability screens are built on;
    the row and every event survive. A scan of the old label returns 410 Gone
    ("this lot was retired"), not 404 ("invalid barcode").

    409 while any stock is still reserved for a cut."""
    return await MaterialService(db).retire_lot(lot_id, actor_id=user.id)


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


# create_order + receive: pass the actor's ROLE so receive can gate the
# mismatch-approval to DM/MD and create_order is consistent.
 
@router.post("/receive", response_model=schemas.ReceiveResult)
async def receive(
    body: schemas.ReceiveRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_DM),
):
    """approved adds to stock; a PO-mismatch is rejected (409) unless a DM/MD
    sends approve_mismatch=true, which receives it into a new substitute lot."""
    return await MaterialService(db).receive(
        body, actor_id=user.id, actor_role=user.role)


@sup_router.post("/orders", response_model=schemas.SupplierOrderResult, status_code=201)
async def create_order(
    body: schemas.SupplierOrderCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_DM),
):
    """Raise a manual supplier order (ORDERED). Validates the requested article
    against the chosen/suggested supplier's catalog."""
    return await MaterialService(db).create_order(
        body, actor_id=user.id, actor_role=user.role)


@sup_router.patch("/orders/{order_id}", response_model=schemas.SupplierOrderPatchResult)
async def mark_arrived(
    order_id: uuid.UUID,
    body: schemas.SupplierOrderPatch,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_DM),
):
    """Flip ORDERED → ARRIVED. Cues the receiving screen. Idempotent."""
    return await MaterialService(db).mark_arrived(order_id)

@sup_router.patch("/orders/{order_id}/spec", response_model=schemas.SupplierOrderPatchResult)
async def edit_order_spec(
    order_id: uuid.UUID,
    body: schemas.OrderSpecPatch,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_DM),     # _DM already = DM + MD; DM/MD only
):
    """Edit an ORDERED order's spec (article/colour/thickness/dcm/qty). DM/MD."""
    res = await MaterialService(db).edit_order_spec(order_id, body, actor_id=user.id)
    order = await MaterialService(db).repo.get_order(order_id)
    return {"order_id": order.id, "status": order.status,
            "arrived_at": order.arrived_at.isoformat() if order.arrived_at else None}