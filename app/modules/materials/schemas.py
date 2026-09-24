"""modules/materials/schemas.py — API contract for materials & suppliers."""
import uuid

from pydantic import BaseModel, Field


class SheetIn(BaseModel):
    """One physical hide arriving, as the tannery measured it.

    `dcm` is the number written on the skin. It is the only required field
    because it is the only one that differs per hide — article, colour and
    thickness are the LOT's, and repeating them per sheet is how the two drift.
    """
    dcm: float = Field(gt=0)
    note: str | None = None


class SheetRead(BaseModel):
    sheet_id: uuid.UUID
    code: str                    # the printed label
    dcm: float
    status: str
    cutting_row_id: uuid.UUID | None = None


class SheetReconciliation(BaseModel):
    """Do the hides add up to the lot's stock figure? Reported, never enforced.

    A lot received before sheet tracking — or a delivery nobody had time to sheet
    — has zero hides and is NOT a mismatch. `reconciled` says so; `difference`
    says by how much when it is one.
    """
    sheets_total: int
    sheets_by_status: dict = Field(default_factory=dict)
    sheet_dcm_in_store: float
    lot_on_hand: float
    difference: float
    reconciled: bool


class SheetDetail(SheetRead):
    """One hide, opened from the stock screen or a scan.

    `editable` is the answer to "may this screen show an Edit button" —
    computed from the same rule the write path enforces, so the button and the
    endpoint cannot disagree.
    """
    material_lot_id: uuid.UUID | None = None
    note: str | None = None
    received_at: str | None = None
    article: str | None = None
    colour: str | None = None
    thickness: str | None = None
    lot_barcode: str | None = None
    editable: bool = True


class SheetPatch(BaseModel):
    """Correct ONE hide. Send what changes.

    Only the two fields that are genuinely this hide's: its measurement and the
    note about it. `status` and `cutting_row_id` are absent on purpose — they
    are moved by allocating, issuing and cutting, which are events, not edits.
    """
    dcm: float | None = Field(default=None, gt=0)
    note: str | None = None


class SheetAdd(BaseModel):
    """Hides missed when the delivery was first sheeted."""
    sheets: list[SheetIn] = Field(min_length=1)


class SheetListResult(BaseModel):
    lot_id: uuid.UUID
    article: str | None = None
    colour: str | None = None
    uom: str | None = None
    count: int = 0
    sheets: list[SheetDetail] = Field(default_factory=list)
    sheets_by_status: dict = Field(default_factory=dict)
    reconciliation: SheetReconciliation | None = None


class SheetAddResult(BaseModel):
    lot_id: uuid.UUID
    added: list[SheetDetail] = Field(default_factory=list)
    reconciliation: SheetReconciliation | None = None
    message: str | None = None


class SheetDeleteResult(BaseModel):
    """A hide removed as a data-entry error.

    `barcode_retired` rather than deleted: a label may already be stuck on
    something, and a scan of it must say "this was removed" (410 Gone) instead
    of "unknown code".
    """
    sheet_id: uuid.UUID
    code: str
    deleted: bool = True
    barcode_retired: bool = False
    lot_id: uuid.UUID | None = None
    reconciliation: SheetReconciliation | None = None
    message: str | None = None


class ArrivalPatch(BaseModel):
    """Correct a PENDING gate entry — and, with the split, complete it.

    `declared_qty` MOVES STOCK — it is the quantity this arrival put on the
    floor — and it is applied as a delta, so a cut made in between is not
    silently undone.

    Send `approved_qty` and/or `rejected_qty` and the arrival is COMPLETED in
    the same call, exactly as POST .../complete would. Send only one and the
    other is worked out from the declared total.

    `article` / `colour` are CHECKED, not changed: they must match the lot the
    arrival landed on. Changing what the material IS moves it to another lot,
    which is a void and a re-entry, not an edit.
    """
    declared_qty: float | None = Field(default=None, gt=0)
    declared_sheet_count: int | None = Field(default=None, gt=0)
    note: str | None = None
    approved_qty: float | None = Field(default=None, ge=0)
    rejected_qty: float | None = Field(default=None, ge=0)
    thickness: str | None = None
    sheets: list[SheetIn] | None = None
    article: str | None = None
    colour: str | None = None


class ArrivalVoidResult(BaseModel):
    receipt_id: uuid.UUID
    voided: bool = True
    lot_id: uuid.UUID | None = None
    qty_removed: float = 0.0
    # The lot is NEVER deleted with its arrival: a barcode may be printed and a
    # recipe may point at it. A lot at zero is an empty shelf, which is true.
    lot_retained: bool = True
    arrived: float = 0.0
    used: float = 0.0
    balance: float = 0.0
    reserved: float = 0.0
    available: float = 0.0
    on_hand: float = 0.0
    message: str | None = None



class StockFigures(BaseModel):
    """THE SIX NUMBERS EVERY MATERIAL READ CARRIES, with one meaning each.

        arrived    everything that ever came in           = balance + used
        used       cut into garments or issued as a kit
        balance    what is on the shelf right now
        reserved   committed to a requirement, NOT yet spent
        available  balance − reserved: what may still be promised
        on_hand    the legacy name for `balance`, kept so existing clients work

    `arrived − used == balance`, always, because arrived is derived from the
    other two. That identity is why these six replaced four figures that did not
    reconcile: `on_hand` meant "arrived" on /materials/stock and "balance" on a
    lot's own page, `reserved` silently included everything already consumed, and
    `used` was declared here but never set by the read — so it rendered 0.0 for
    every lot in the building however much of it had been cut.

    SHEET-WISE IS THE SAME QUESTION IN HIDES, and it is a different answer: a
    cutter is handed skins, and "how many are on the shelf" cannot be worked out
    from a sum of decimetres. Empty for anything that is not leather, which is
    honest — lining is metres and a button is a button.
    """
    arrived: float = 0.0
    used: float = 0.0
    balance: float = 0.0
    reserved: float = 0.0
    available: float = 0.0
    on_hand: float = 0.0

    # hides, bucketed by where they are in their life
    sheets_arrived: int = 0
    sheets_arrived_dcm: float = 0.0
    sheets_balance: int = 0            # IN_STOCK + RETURNED — on the shelf
    sheets_balance_dcm: float = 0.0
    sheets_allocated: int = 0          # ALLOCATED + ISSUED — out, not yet cut
    sheets_allocated_dcm: float = 0.0
    sheets_used: int = 0               # CONSUMED
    sheets_used_dcm: float = 0.0
    sheets_scrapped: int = 0
    sheets_scrapped_dcm: float = 0.0
    sheets_by_status: dict = Field(default_factory=dict)

    # Deliveries recorded at the gate that nobody has finished entering. Their
    # quantity is already inside `arrived` and is cuttable — it is PROVISIONAL
    # until the approved/rejected split is entered, and a stock figure that
    # cannot say which part of itself is provisional is one people stop trusting.
    pending_arrivals: int = 0
    pending_arrival_qty: float = 0.0


# ── arrivals: the delivery entered in two sittings ──────────────────────────
class ArrivalCreate(BaseModel):
    """What the person signing for the van actually knows.

    Article, colour, how much. Everything else — the approved/rejected split, the
    thickness off the packing note, the measurement on each hide — is entered at
    POST /materials/arrivals/{receipt_id}/complete whenever there is time.

    The quantity goes into stock immediately and is CUTTABLE, because the leather
    is physically in the building and the floor is not going to wait for QC. It
    is marked provisional everywhere it is read until the arrival is completed.
    """
    article: str
    colour: str
    # In the category's own unit: dcm for leather, mtrs/kg/pcs otherwise.
    total_qty: float = Field(gt=0)
    # OPTIONAL. "Twelve bundles came in" is often known at the gate even when
    # nobody has measured one. It is what the completion's hide count is checked
    # against — reported on a mismatch, never enforced.
    sheet_count: int | None = Field(default=None, gt=0)
    category: str = "LEATHER"
    subtype: str | None = None
    # Not required at the gate. Supplying it picks out the right lot when one
    # article/colour comes in more than one thickness; omitting it means the
    # arrival lands on the lot that has no thickness recorded, and `complete`
    # can fill it in.
    thickness: str | None = None
    size: str | None = None
    supplier_id: uuid.UUID | None = None
    supplier_order_id: uuid.UUID | None = None
    note: str | None = None


class ArrivalResult(StockFigures):
    receipt_id: uuid.UUID
    lot_id: uuid.UUID
    lot_barcode: str | None = None
    lot_created: bool = False
    category: str | None = None
    subtype: str | None = None
    article: str | None = None
    colour: str | None = None
    thickness: str | None = None
    uom: str | None = None
    declared_qty: float = 0.0
    declared_sheet_count: int | None = None
    status: str = "PENDING"
    # The field names the completion form still has to collect, in order.
    outstanding: list[str] = Field(default_factory=list)
    message: str | None = None


class ArrivalComplete(BaseModel):
    """The second sitting. What was approved, what went back, and the hides."""
    approved_qty: float = Field(ge=0)
    rejected_qty: float = Field(ge=0, default=0)
    # LEATHER: one entry per physical hide, each with the number written on it.
    sheets: list[SheetIn] | None = None
    # Fill in the one identity field a gate entry usually cannot supply, rather
    # than needing a separate PATCH that nobody makes.
    thickness: str | None = None
    note: str | None = None


class ArrivalRow(BaseModel):
    """One line of the come-back-to-it queue."""
    receipt_id: uuid.UUID
    lot_id: uuid.UUID
    status: str
    article: str | None = None
    colour: str | None = None
    thickness: str | None = None
    category: str | None = None
    uom: str | None = None
    declared_qty: float = 0.0
    declared_sheet_count: int | None = None
    approved_qty: float = 0.0
    rejected_qty: float = 0.0
    supplier_order_id: uuid.UUID | None = None
    note: str | None = None
    arrived_at: str | None = None
    completed_at: str | None = None
    outstanding: list[str] = Field(default_factory=list)
    # Filled only on a PATCH that completed the arrival.
    on_hand_delta: float | None = None
    sheets: list[SheetRead] = Field(default_factory=list)
    warnings: list[dict] = Field(default_factory=list)


class ArrivalList(BaseModel):
    count: int
    total: int
    status: str | None = None
    arrivals: list[ArrivalRow] = Field(default_factory=list)


class ArrivalCompleteResult(StockFigures):
    receipt_id: uuid.UUID
    lot_id: uuid.UUID
    status: str
    declared_qty: float = 0.0
    approved_qty: float = 0.0
    rejected_logged: float = 0.0
    # approved − declared: how much the QC split moved the provisional figure.
    on_hand_delta: float = 0.0
    sheets: list[SheetRead] = Field(default_factory=list)
    sheet_reconciliation: SheetReconciliation | None = None
    warnings: list[dict] = Field(default_factory=list)


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
    # LEATHER only. One entry per physical hide, each with its own dcm. Omit
    # it and the lot behaves exactly as it does today — a stock figure with no
    # per-hide detail — so nothing already on the floor changes.
    sheets: list[SheetIn] | None = None


class LotRead(StockFigures):
    """One row of the lot picker — everything a cut screen needs to choose.

    Carries the full arrived/used/balance block (see StockFigures) in dcm and in
    hides, so the picker can grey out a lot that cannot cover the cut without a
    second call per row.
    """
    lot_id: uuid.UUID
    barcode: str | None          # the printed label; None = never registered
    category: str
    subtype: str | None
    article: str
    colour: str | None
    thickness: str | None
    size: str | None
    uom: str
    last_used_for_sku: bool = False   # pre-select this one, but SHOW that you did
    covers_required: bool | None = None   # null unless `required` was passed
    received: float = 0.0             # BUG #26 — everything that ever arrived
    rejected: float = 0.0
    sheets_total: int = 0             # #18 — hides taken into this lot


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


class LotCreateResult(StockFigures):
    lot_id: uuid.UUID
    lot_barcode: str
    # The hide labels to print, in the order they were created. Returned on the
    # create so the store can print the sheet stickers in the same breath as the
    # lot sticker instead of going looking for them.
    sheets: list[SheetRead] = Field(default_factory=list)
    category: str
    subtype: str | None
    article: str
    colour: str | None
    uom: str


# ── lot CRUD (change-list item 7) ───────────────────────────────────────────
class LotDetail(StockFigures):
    """One lot, opened from the stock screen.

    `used` USED TO BE A LIE ON THIS SHAPE: it was declared with a default of 0.0
    and the read never set it, so every lot reported nothing used however much of
    it had been cut. It now comes from lot.used, the same column every cut and
    every kit issue moves.
    """
    lot_id: uuid.UUID
    barcode: str | None = None
    category: str
    subtype: str | None = None
    article: str
    colour: str | None = None
    thickness: str | None = None
    size: str | None = None
    uom: str
    # WHAT WAS BOUGHT, beside what arrived. `received` sums the delivery rows and
    # answers "how much of this have we ever paid for"; `arrived` is the ledger's
    # own balance + used. They agree unless a lot was hand-adjusted — and seeing
    # them diverge is exactly how an adjustment gets noticed.
    received: float = 0.0
    rejected: float = 0.0     # supplier quality history
    deliveries: int = 0       # how many times this lot has been received into
    # #18 — hides in this lot, for the lot directory.
    sheets_total: int = 0
    attributes: dict = Field(default_factory=dict)
    supplier_id: uuid.UUID | None = None
    is_active: bool = True
    # So the edit form renders this material's fields without a second call to
    # GET /materials/spec.
    editable_fields: list[str] = Field(default_factory=list)
    required_attributes: list[str] = Field(default_factory=list)


class LotPatch(BaseModel):
    """Correct a lot's IDENTITY. Send only what changes.

    category / subtype / uom / on_hand are absent on purpose — see the route
    docstring for why each one is a different operation, not a field edit.
    """
    article: str | None = None
    colour: str | None = None
    thickness: str | None = None
    size: str | None = None
    supplier_id: uuid.UUID | None = None


class LotAdjust(BaseModel):
    """A counted stock correction. `delta` is the MOVEMENT, not the new total —
    +12 adds twelve, -12 removes twelve. The reason is stored on the audit row
    and is the only record of why the count and the system disagreed."""
    delta: float
    reason: str = Field(min_length=3, max_length=300)


class LotRetireResult(BaseModel):
    lot_id: uuid.UUID
    is_active: bool
    barcode_retired: bool
    on_hand: float
    history_preserved: bool = True
    message: str = ""


class StockRead(StockFigures):
    """The DM's stock check, summed over every lot the filter matched.

    `on_hand` here used to mean ARRIVED while `on_hand` on a lot's own page meant
    BALANCE, so the two screens could not both be read as the same quantity. It
    means balance everywhere now — read `arrived` / `used` / `balance`.
    """
    category: str | None
    subtype: str | None
    article: str | None
    colour: str | None
    thickness: str | None
    size: str | None
    uom: str
    lot_count: int
    required: float | None = None
    short_by: float | None = None
    suggested_supplier: dict | None = None


class ReceiveRequest(BaseModel):
    lot_id: uuid.UUID
    supplier_order_id: uuid.UUID | None = None
    # OPTIONAL. Everything that came off the van, approved + rejected, in the
    # lot's own unit — the same figure POST /materials/arrivals calls total_qty.
    # Send it and rejected_qty may be left out: it is worked out as
    # total − approved. Send all three and they must add up (422 otherwise).
    total_qty: float | None = Field(default=None, gt=0)
    # OPTIONAL. The bundle/hide count taken at the gate. Checked against the
    # hides in `sheets` — reported on a mismatch, never enforced.
    sheet_count: int | None = Field(default=None, gt=0)
    # OPTIONAL when total_qty is sent. Leave BOTH approved_qty and rejected_qty
    # out and the delivery is recorded as a PENDING arrival: total_qty goes into
    # stock provisionally and the split is entered later at
    # PATCH /materials/arrivals/{receipt_id}. Send only rejected_qty and
    # approved is worked out as total − rejected.
    approved_qty: float | None = Field(default=None, ge=0)
    rejected_qty: float = Field(ge=0, default=0)
    reserve_for_required: float | None = None
    approve_mismatch: bool = False          # NEW: DM/MD accept a substitution
    # LEATHER only. The hides in THIS delivery, each with its measurement.
    sheets: list[SheetIn] | None = None


class ReceiveResult(StockFigures):
    lot_id: uuid.UUID
    # The delivery row — what PATCH /materials/receipts/{receipt_id} corrects.
    receipt_id: uuid.UUID | None = None
    # COMPLETED when the split was sent; PENDING when it is still owed — finish
    # it at PATCH /materials/arrivals/{receipt_id}. `outstanding` names what is.
    status: str = "COMPLETED"
    outstanding: list[str] = Field(default_factory=list)
    message: str | None = None
    total_qty: float = 0.0                 # approved + rejected, this delivery
    approved_qty: float = 0.0
    rejected_logged: float
    sheet_count: int | None = None          # as declared; None if not sent
    sheets_entered: int = 0                 # hides actually measured in `sheets`
    supplier_order_status: str | None
    substituted: bool = False               # NEW: received into a NEW lot
    mismatch_fields: list[str] | None = None  # NEW: which fields differed
    sheets: list[SheetRead] = Field(default_factory=list)   # labels to print
    sheet_reconciliation: SheetReconciliation | None = None
    warnings: list[dict] = Field(default_factory=list)


class ReceiptPatch(BaseModel):
    """Correct ONE delivery's approved/rejected split. Send what changes.

    Same rules as POST /materials/receive: send `total_qty` without
    `rejected_qty` and rejected is worked out as total − approved; send all three
    and they must add up. A change to approved MOVES STOCK by the difference.
    """
    approved_qty: float | None = Field(default=None, ge=0)
    rejected_qty: float | None = Field(default=None, ge=0)
    total_qty: float | None = Field(default=None, gt=0)
    sheet_count: int | None = Field(default=None, gt=0)
    reason: str = Field(min_length=3, max_length=300)


class ReceiptAdjustResult(StockFigures):
    receipt_id: uuid.UUID
    lot_id: uuid.UUID
    status: str
    total_qty: float = 0.0
    approved_qty: float = 0.0
    rejected_qty: float = 0.0
    sheet_count: int | None = None
    stock_delta: float = 0.0            # new approved − old approved
    before: dict = Field(default_factory=dict)
    reason: str
    message: str | None = None


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


class ReceiptRow(BaseModel):
    """One delivery against a lot — the purchase history (#16).

    The rows have existed since receiving was built; there was simply no way to
    read them back, so "what did we buy and when" was unanswerable from the app.
    """
    receipt_id: uuid.UUID
    status: str | None = None
    total_qty: float | None = None      # as declared at receiving
    approved_qty: float
    rejected_qty: float
    sheet_count: int | None = None
    supplier_order_id: uuid.UUID | None = None
    received_by: uuid.UUID | None = None
    received_at: str | None = None


class LotHistory(BaseModel):
    lot_id: uuid.UUID
    article: str
    colour: str | None = None
    uom: str
    on_hand: float
    received: float
    rejected: float
    receipts: list[ReceiptRow] = Field(default_factory=list)


class MaterialSpecRead(BaseModel):
    """Which boxes the Add-New form and the stock filters must render.

    Drives the UI per (category, subtype) so the form matches the material class
    exactly — see the STRICT per-category table in CLAUDE.md s5.
    """
    category: str | None = None
    subtype: str | None = None
    filters: list[str]
    required_to_add: list[str]
    quantity_field: str | None = None
    uom: str | None = None


class LeatherByStyleRow(BaseModel):
    """Arrived / consumed / available for one style.

    The consumed figure is SPLIT BY REWORK, so "this style cost X, of which Y
    was defects" is answerable rather than inferred.
    """
    style_id: uuid.UUID
    style_name: str | None = None
    style_article: str | None = None
    article: str | None = None
    colour: str | None = None
    uom: str | None = None
    pieces: int = 0
    arrived: float = 0.0
    consumed: float = 0.0
    consumed_rework: float = 0.0
    consumed_original: float = 0.0
    on_hand: float = 0.0
    reserved: float = 0.0
    available: float = 0.0


class PieceSheetRow(BaseModel):
    code: str
    dcm: float = 0.0
    status: str | None = None


class PieceConsumption(BaseModel):
    """What ONE garment took, hide by hide.

    A piece cut through the grid lists its actual sheets. One cut the old way (a
    typed dcm) lists none, which is the honest answer: nobody recorded which
    hides those were.
    """
    piece_id: uuid.UUID
    total: float = 0.0
    rework: float = 0.0
    original: float = 0.0
    events: list[dict] = Field(default_factory=list)
    sheets: list[PieceSheetRow] = Field(default_factory=list)


class ManualIssueResult(BaseModel):
    """A material recorded as issued to a garment OUTSIDE its spec."""
    piece_code: str
    article: str | None = None
    colour: str | None = None
    qty: float
    uom: str | None = None
    source: str
    available_after: float | None = None
    # Present when the issue spent more than the ledger held. Warn, never block.
    stock_warning: dict | None = None
    message: str
