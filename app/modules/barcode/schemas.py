"""modules/barcode/schemas.py — API contract for the barcode registry."""
from datetime import datetime
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BarcodeResolve(BaseModel):
    """Loose by design: resolve() returns a type + a type-specific sub-object, and
    the frontend branches on `type`. Kept as a passthrough dict rather than a
    rigid union so a new barcode type never breaks the read contract."""
    code: str
    type: str
    active: bool
    caption: str | None = None
    piece: dict | None = None
    employee: dict | None = None
    drawer: dict | None = None
    lot: dict | None = None
    # True when this is a legacy long piece code kept scannable after the
    # compact-code switch (bug #19) — the scan works, the label wants reprinting.
    is_alias: bool = False
    # "PIECE" / "DRAWER" / null — which code to present next. Answered for EVERY
    # barcode type, including EMPLOYEE (the first scan of every workflow, which
    # used to come back null). Guidance; store_scan remains the authority.
    next_expected_scan: str | None = None
    # The production stage this piece is due at, derived from its completed
    # operations by the same helper the write path uses. Null once the piece has
    # finished the chain, or for a code that names no piece.
    next_stage: str | None = None
    next_stage_label: str | None = None
    # Set when that stage cannot be logged yet — e.g. the drawer has not been sent.
    # The stage is still reported: the screen shows where the piece is going AND
    # what is holding it there.
    next_stage_blocked_reason: str | None = None


class PrintRequest(BaseModel):
    codes: list[str] | None = None
    sku_id: uuid.UUID | None = None
    order_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _one_source(self):
        if not self.codes and not self.sku_id and not self.order_id:
            raise ValueError("Provide codes, sku_id, or order_id.")
        return self


class PrintLabel(BaseModel):
    """One sticker: a small symbol plus the text printed under it (bug #19)."""
    code: str            # encode THIS — the compact PC-… id
    symbology: str
    caption: str
    known: bool
    # {order_number, article, style, colour, size, serial, piece_code}. Null for
    # non-piece labels (drawer / employee / lot), which name no garment.
    details: dict | None = None
    label_line: str | None = None   # the same fields pre-joined, ready to typeset


class PrintResponse(BaseModel):
    labels: list[PrintLabel]


class BarcodeAction(BaseModel):
    action: str = Field(pattern="^(deactivate|reissue)$")


class BarcodeActionResult(BaseModel):
    employee_id: uuid.UUID
    employee_barcode: str
    active: bool
    history_preserved: bool
    
class OrderPickerRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    order_id: uuid.UUID
    order_number: str
    client_name: str
    minted: int
    first_generated_at: datetime | None = None
    last_generated_at: datetime | None = None
 
 
class StyleAnalyticsRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    style_id: uuid.UUID
    style_name: str
    style_code: str | None = None
    planned: int
    minted: int
    balance: int
 
 
class OrderTotal(BaseModel):
    planned: int
    generated: int
    balance: int
    active: int
    retired: int
    duplicates: int
    half_minted: bool
    fully_generated: bool
 
 
class OrderAnalytics(BaseModel):
    order_id: uuid.UUID
    order_total: OrderTotal
    by_style: list[StyleAnalyticsRow]
 
 
class BarcodeHistoryRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    code: str                        # the compact scannable id
    status: str
    sku_code: str | None = None
    style_name: str | None = None
    article: str | None = None       # bug #7/#19
    serial: str | None = None        # "001" — the zero-padded seq (bug #7)
    piece_code: str | None = None    # the long human identity, for reference
    colour: str | None = None
    size: str | None = None
    seq: int | None = None
    current_stage: str | None = None
    generated_at: datetime | None = None
 
 
class BarcodeHistoryPage(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    order_id: uuid.UUID
    page: int
    page_size: int
    total: int
    pages: int
    items: list[BarcodeHistoryRow]
 
 
class OrderSkuOption(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    sku_id: uuid.UUID
    sku_code: str | None = None
    colour: str | None = None
    size: str | None = None
    style_id: uuid.UUID
    style_name: str | None = None