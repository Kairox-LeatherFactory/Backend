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
    # WHICH HIDE A TYPED MEASUREMENT RESOLVED TO. Only present on the response to
    # POST /rows/{id}/sheets — the operator typed a number and never chose a
    # code, so the reply has to say which skin actually left the shelf.
    matched_sheet: dict | None = None


class SheetOption(BaseModel):
    """One hide the row could take, as the dcm box offers it."""
    sheet_id: uuid.UUID
    code: str
    dcm: float
    status: str
    material_lot_id: uuid.UUID | None = None
    # Signed gap from the dcm the caller asked about; None when none was given.
    difference: float | None = None


class SheetOptions(BaseModel):
    """What is on the shelf for this row — the lookup behind the dcm box."""
    row_id: uuid.UUID
    article: str | None = None
    colour: str | None = None
    requested_dcm: float | None = None
    available_count: int = 0
    sheets: list[SheetOption] = Field(default_factory=list)
    lots: list[dict] = Field(default_factory=list)


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
    # NO cutter HERE, and its absence is the contract.
    #
    # `generate` mints ONE ROW PER GARMENT for a whole style+colour — routinely
    # 40+ rows. A cutter on this body was therefore a cutter on EVERY row of the
    # style, which says "one person cut all forty jackets". That is not how the
    # floor works and it is not a cosmetic error: cutter_employee_id is what the
    # piece-rate wage line is paid from (see approve()), so one id here pays one
    # worker for everybody's work.
    #
    # The cutter is named per garment, where the garment is actually handed over:
    #   · POST /cutting/rows/{id}/approve  {"cutter_employee_id": "…"}   ← primary
    #   · PATCH /cutting/rows/{id}         {"cutter_employee_id": "…"}   ← editing
    #
    # The field is still DECLARED so a client that has not been updated gets a
    # 422 naming those two routes (CuttingService.generate) instead of having its
    # cutter silently dropped by Pydantic's extra-field handling — a dropped
    # cutter is a whole style approved with nobody on it.
    cutter_employee_id: uuid.UUID | None = Field(
        default=None,
        description="REMOVED — assign the cutter per row at "
                    "POST /cutting/rows/{row_id}/approve. Sending it here is a 422.")
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
    """Put a hide on the row — by measurement, by barcode, or by id.

    `dcm` IS THE PRIMARY DOOR and it is a LOOKUP: the cutter reads the number
    written on the skin, types it, and the system resolves it to the hide of this
    row's article and colour that measures that — no code to find, no list to
    pick from. Exact match first, then the nearest hide within `dcm_tolerance`;
    outside that the call is refused and names the hides that ARE on the shelf.

    It used to CREATE a hide instead, which put stock in that nobody had
    received. That is now `create_if_missing`, so adding stock is asked for
    rather than being what a mistyped measurement does.
    """
    sheet_code: str | None = None
    sheet_id: uuid.UUID | None = None
    # Narrows the lookup (and names the lot on a create). Omitted, the row's own
    # hides say which lot the cutter is working out of, then its article+colour.
    material_lot_id: uuid.UUID | None = None
    dcm: float | None = Field(default=None, gt=0)
    # How far off the typed number a hide may be and still be "the one he means".
    # Omitted → 2% of the typed value, never tighter than 1 dcm.
    dcm_tolerance: float | None = Field(default=None, gt=0)
    # THE DELIVERY NOBODY SHEETED. True mints a new hide at this dcm when the
    # shelf has nothing that matches. It ADDS a stock row, so it is deliberate.
    create_if_missing: bool = False


class SheetPatch(BaseModel):
    dcm: float = Field(gt=0)


class ApproveRequest(BaseModel):
    """Who cut THIS garment. Optional only because the row may already say.

    THE CUTTER IS NAMED HERE, one row at a time, because approval is the moment
    the manager is actually looking at one garment and its hides. `generate`
    deliberately takes no cutter (see GenerateRequest) — a single id across a
    whole style pays one worker for forty people's work.

    Sending it re-assigns the row before it freezes, so the common path is one
    call per garment: approve with the cutter who cut it. Omitting it keeps
    whatever the row already carries (set at PATCH), and a row with no cutter at
    all is still a 409 — the wage line cannot be filled in afterwards without
    rewriting a signed-off row.
    """
    cutter_employee_id: uuid.UUID | None = None


class ApproveResult(BaseModel):
    row: RowRead
    message: str


class RowAssignment(BaseModel):
    row_id: uuid.UUID
    cutter_employee_id: uuid.UUID | None = None


class BulkAssignRequest(BaseModel):
    """Assign cutters to several rows in one call — the grid's 'save' button.

    ONE ENTRY PER ROW, so different pieces of the same style go to different
    people. This is the bulk form of PATCH /cutting/rows/{id}; it exists because
    the manager fills the cutter column for a screenful of garments at once and
    forty PATCHes is forty chances to lose half of them.

    PARTIAL ACCEPT, like every other batch surface here: a frozen or missing row
    comes back in `rejected` and never loses the rows that did assign.
    """
    assignments: list[RowAssignment] = Field(default_factory=list)


class BulkAssignResult(BaseModel):
    assigned: int = 0
    rows: list[RowRead] = Field(default_factory=list)
    rejected: list[dict] = Field(default_factory=list)
