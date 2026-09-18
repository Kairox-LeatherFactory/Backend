"""HTTP shapes for the cutting grid. Router speaks these; the service never does."""
import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field


class SheetCell(BaseModel):
    """One hide on a row — one cell of the Excel's sheet columns."""
    sheet_id: uuid.UUID
    code: str
    dcm: float
    status: str


class RowRead(BaseModel):
    """One garment's row, as the grid renders it."""
    row_id: uuid.UUID
    piece_id: uuid.UUID | None
    piece_code: str | None = None
    style_id: uuid.UUID | None
    sku_id: uuid.UUID | None
    article: str | None
    colour: str | None
    size: str | None
    rc_no: str | None
    cutter_employee_id: uuid.UUID | None
    cutter_name: str | None = None
    work_date: date | None
    status: str
    # What the allocator aimed at and whose number it was — "style_spec" (a
    # measurement somebody signed off) or "size_baseline" (this system's
    # estimate). The screen shows the difference so the manager knows whether to
    # expect to correct it.
    target_dcm: float | None
    target_source: str | None
    total_dcm: float | None          # DERIVED from the sheets below
    sheets: list[SheetCell] = Field(default_factory=list)
    sheet_count: int = 0
    approved_at: datetime | None = None
    logged_at: datetime | None = None
    # Non-blocking notes: short of stock, over/under the target, no cutter set.
    warnings: list[str] = Field(default_factory=list)


class CuttingGrid(BaseModel):
    """The whole sheet for one style+colour, plus what the header needs."""
    style_id: uuid.UUID | None
    style_name: str | None
    colour: str | None
    colours: list[str] = Field(default_factory=list)
    rows: list[RowRead] = Field(default_factory=list)
    # Cutters actually in the factory today. The grid must not offer a name that
    # cannot legally be logged against — production refuses an absent worker.
    present_cutters: list[dict] = Field(default_factory=list)
    uncut_pieces: int = 0
    warnings: list[str] = Field(default_factory=list)


class GenerateRequest(BaseModel):
    style_id: uuid.UUID
    colour: str | None = None
    # The lot to draw hides from. Omitted, the service picks the only leather lot
    # matching the style's article+colour, and 409s when several match rather
    # than guessing which hides to spend.
    material_lot_id: uuid.UUID | None = None
    limit: int | None = Field(default=None, gt=0)
    work_date: date | None = None
    cutter_employee_id: uuid.UUID | None = None
    # False generates the rows with no hides attached, for a manager who would
    # rather scan them himself.
    allocate: bool = True


class GenerateResult(BaseModel):
    created: int
    rows: list[RowRead] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class RowPatch(BaseModel):
    """Every cell the manager may edit. All optional; omitted means unchanged."""
    cutter_employee_id: uuid.UUID | None = None
    size: str | None = None
    rc_no: str | None = None
    article: str | None = None
    colour: str | None = None
    work_date: date | None = None
    note: str | None = None


class SheetAdd(BaseModel):
    """Put a hide on the row — by barcode (scanned) or id (picked).

    `dcm` creates a NEW hide against the lot instead of claiming an existing one,
    for the delivery that was never sheeted at receiving.
    """
    sheet_code: str | None = None
    sheet_id: uuid.UUID | None = None
    material_lot_id: uuid.UUID | None = None
    dcm: float | None = Field(default=None, gt=0)


class SheetPatch(BaseModel):
    dcm: float = Field(gt=0)


class ApproveResult(BaseModel):
    row: RowRead
    message: str
