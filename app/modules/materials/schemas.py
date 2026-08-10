"""modules/materials/schemas.py — API contract for materials & suppliers."""
import uuid

from pydantic import BaseModel, Field


class LotCreate(BaseModel):
    category: str                       # LEATHER | LINING | ACCESSORY
    subtype: str | None = None          # LINING: PLAIN_LINING/RIBS/KNIT
    #                                     ACCESSORY: BUTTON/ZIP/THREAD/OTHER (required)
    article: str
    colour: str | None = None
    # STRICT per-category attribute keys (validated on create, else 422):
    #   LEATHER            → {thickness, dcm}
    #   LINING/PLAIN_LINING→ {thickness, mtrs}
    #   LINING/RIBS        → {kg}
    #   LINING/KNIT        → {pcs}
    #   ACCESSORY/BUTTON   → {size, count}
    #   ACCESSORY/ZIP      → {size, count}
    #   ACCESSORY/THREAD   → {thickness, mtrs}
    #   ACCESSORY/OTHER    → {description, count}
    # The quantity is read from the category's own field (dcm/mtrs/kg/pcs/count);
    # call GET /materials/spec?category=&subtype= to get this list at runtime.
    attributes: dict = Field(default_factory=dict)
    supplier_id: uuid.UUID | None = None


class LotRead(BaseModel):
    """One row of the lot picker — everything a cut screen needs to choose."""
    lot_id: uuid.UUID
    barcode: str | None          # the printed label; None = never registered
    category: str
    subtype: str | None
    article: str
    colour: str | None
    thickness: str | None
    size: str | None
    uom: str
    on_hand: float
    reserved: float
    available: float             # on_hand − reserved
    last_used_for_sku: bool = False   # pre-select this one, but SHOW that you did
    covers_required: bool | None = None   # null unless `required` was passed


class LotOptions(BaseModel):
    """Distinct values across the matched lots — fills the cascading dropdowns.
    `/materials/spec` says WHICH boxes to render; this says what goes in them."""
    article: list[str] = Field(default_factory=list)
    colour: list[str] = Field(default_factory=list)
    thickness: list[str] = Field(default_factory=list)
    size: list[str] = Field(default_factory=list)


class LotListResult(BaseModel):
    count: int
    lots: list[LotRead]
    options: LotOptions
    suggested_lot_id: uuid.UUID | None = None   # derived last-used for the SKU
    required: float | None = None


class LotCreateResult(BaseModel):
    lot_id: uuid.UUID
    lot_barcode: str
    category: str
    subtype: str | None
    article: str
    colour: str | None
    on_hand: float
    reserved: float
    available: float
    uom: str


class StockRead(BaseModel):
    category: str | None
    subtype: str | None
    article: str | None
    colour: str | None
    thickness: str | None
    size: str | None
    uom: str
    on_hand: float
    reserved: float
    available: float
    lot_count: int
    required: float | None = None
    short_by: float | None = None
    suggested_supplier: dict | None = None


class ReceiveRequest(BaseModel):
    lot_id: uuid.UUID
    supplier_order_id: uuid.UUID | None = None
    approved_qty: float = Field(ge=0)
    rejected_qty: float = Field(ge=0, default=0)
    reserve_for_required: float | None = None
    approve_mismatch: bool = False          # NEW: DM/MD accept a substitution


class ReceiveResult(BaseModel):
    lot_id: uuid.UUID
    on_hand: float
    reserved: float
    available: float
    rejected_logged: float
    supplier_order_status: str | None
    substituted: bool = False               # NEW: received into a NEW lot
    mismatch_fields: list[str] | None = None  # NEW: which fields differed


class OrderSpecPatch(BaseModel):            # NEW: DM/MD edit an order's spec
    article: str | None = None
    colour: str | None = None
    thickness: str | None = None
    dcm: float | None = None
    qty: float | None = Field(default=None, gt=0)

class SupplierOrderCreate(BaseModel):
    category: str
    subtype: str | None = None
    article: str
    colour: str | None = None
    qty: float = Field(gt=0)
    thickness: str | None = None # new
    dcm: float | None = None # for leather and lining
    supplier_id: uuid.UUID | None = None


class SupplierOrderResult(BaseModel):
    order_id: uuid.UUID
    status: str
    article: str
    qty: float
    uom: str
    supplier: dict | None = None


class SupplierOrderPatch(BaseModel):
    status: str = Field(pattern="^(arrived|ARRIVED)$")


class SupplierOrderPatchResult(BaseModel):
    order_id: uuid.UUID
    status: str
    arrived_at: str | None