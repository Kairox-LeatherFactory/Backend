"""HTTP shapes for the store. One scan fewer than the drawer flow had."""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StoreScanRequest(BaseModel):
    """Scan the worker, scan the garment — and for an accessory, scan the packet.

    THE DRAWER SCAN IS GONE, and removing it is the feature. The old flow was
    employee → DRAWER → piece, with a 409 when the piece was not the one that
    drawer had been assigned at upload. That third scan existed only to find a
    numbered box; the box is gone.

    A LEATHER OR LINING SCAN IS STILL TWO SCANS and `part` is still inferred from
    the garment's own history — the operator should not be asked a question the
    system can answer.

    AN ACCESSORY IS THREE, AND THE THIRD IS THE PACKET'S OWN LABEL. `lot_barcode`
    (LOT-ACC-…) is what says which recipe line this scan is issuing, and it is
    now the ONLY way to issue one. Sending `part: "ACCESSORY"` without a packet
    is a 422: the blanket kit scan spent every accessory line the recipe named
    from a single tap, so nothing in the exchange involved the physical packets
    and an M packet in an L jacket could not be detected at all.

    THERE IS NO `qty`, AND THERE IS NOTHING TO TYPE (Hamthan, 2026-09-29). The two
    barcodes already determine the quantity: the PIECE code gives its SKU, whose
    recipe was declared at the breakdown release, and the PACKET label gives the
    article, colour and size. `match_packet` intersects them to find the one line,
    and that line's `qty_per_piece` IS the number. Asking the operator for it asked
    them to restate a number the system holds — and let them restate it wrong. It
    also fanned out: one `qty` with three packets issued that quantity of EVERY one.

    `extra="forbid"`, SO THE REMOVAL IS LOUD. Pydantic's default is to ignore an
    unknown field, which would have given a screen still sending `qty` a cheerful
    201 with the value silently dropped. A 422 naming the field is how the frontend
    learns.
    """
    model_config = ConfigDict(extra="forbid")

    employee_barcode: str | None = None
    employee_id: uuid.UUID | None = None
    piece_barcode: str | None = None
    piece_id: uuid.UUID | None = None
    part: str | None = None            # LEATHER | LINING (ACCESSORY: scan the lot)
    # The accessory packet. Scanning one IS the accessory scan — `part` may be
    # omitted, and a `part` that says anything else is a 422.
    lot_barcode: str | None = None
    lot_id: uuid.UUID | None = None
    # SEVERAL PACKETS IN ONE CALL. A garment takes everything the DM put on the
    # release breakdown — button, zip, thread and more — and all of it must be in
    # before the garment can leave the store, so a screen that has collected the
    # scans submits them together. A scan gun that fires per beep keeps using the
    # singular field; it is simply a one-element batch on the way in.
    #
    # PARTIAL ACCEPT: if one packet is the wrong size the others still issue and the
    # bad one comes back in `accessory_batch.refused`, because one bad packet must
    # never lose the good ones scanned with it.
    lot_barcodes: list[str] | None = None
    lot_ids: list[uuid.UUID] | None = None
    # Why this packet, when its size is not the garment's. Carried on the approval
    # request so the DM reading the queue is not guessing.
    substitution_reason: str | None = None

    @model_validator(mode="after")
    def _need_both(self):
        if not (self.employee_barcode or self.employee_id):
            raise ValueError("Scan the worker's card first.")
        if not (self.piece_barcode or self.piece_id):
            raise ValueError("Scan the garment.")
        # EXACTLY ONE WAY OF NAMING THE PACKETS. Two of them cannot be reconciled,
        # and silently preferring one is how the wrong packet gets issued — the same
        # rule the recipe fan-out applies to its scopes.
        named = [n for n, v in (("lot_barcode", self.lot_barcode),
                                ("lot_id", self.lot_id),
                                ("lot_barcodes", self.lot_barcodes),
                                ("lot_ids", self.lot_ids)) if v]
        if len(named) > 1:
            raise ValueError(
                f"This scan names its packets more than once ({', '.join(named)}). "
                f"Send exactly one of lot_barcode, lot_id, lot_barcodes or lot_ids.")
        return self


class StoreScanResult(BaseModel):
    piece_code: str
    store_state: str
    holding: str                 # HOLDING LEATHER / LINING / BOTH / EMPTY
    leather_in: bool
    lining_in: bool
    accessories_in: bool
    part_stored: str | None
    part_inferred: bool
    complete: bool
    # What this garment is still owed — LEATHER / LINING / ACCESSORY. Returned so
    # the screen never has to re-implement the completeness rule; a second
    # implementation is a second implementation that can disagree.
    awaiting: list[str] = Field(default_factory=list)
    # True when THIS scan completed the garment and it received itself.
    auto_received: bool = False
    # Alias of `complete`, kept because that is what the store screen calls it.
    ready_for_received: bool = False
    # Has it left the store? A garment can be complete and still on the shelf.
    sent: bool = False
    needs_lining: bool
    lining_reason: str | None = None
    # THE WHOLE CHECKLIST, and it rides on every scan — not just the accessory one.
    # `declared` / `scanned` / `pending` / `progress` / `pending_line` are the same
    # three-way split GET /store/pieces/{code} returns, from the same computation.
    kit: dict | None = None
    # Set only when this scan spent a DM-APPROVED wrong-size packet. A refused one
    # never reaches a 201 — it is a 409 with the request id to approve.
    substitution: dict | None = None
    # Per-packet outcomes for an accessory scan; null for leather/lining.
    accessory_batch: dict | None = None
    next_action: str
    warnings: list[str] = Field(default_factory=list)


class SubstitutionDecision(BaseModel):
    """A DM/MD answering one wrong-size ask. Approving does NOT issue anything.

    The operator is holding the packet and the DM is not, so the DM grants
    permission and the operator re-scans — which is what writes the stock movement
    against the worker's own card. An approval that spent stock by itself would
    record a manager as having issued a packet they never touched.
    """
    note: str | None = None


class SubstitutionResult(BaseModel):
    request_id: str
    status: str                      # PENDING | APPROVED | REJECTED | CONSUMED
    piece_code: str | None = None
    article: str | None = None
    garment_size: str | None = None
    lot_size: str | None = None
    decided_by: str | None = None
    decision_note: str | None = None
    message: str


class SubstitutionList(BaseModel):
    count: int
    total: int = 0
    pending: int = 0
    limit: int = 200
    offset: int = 0
    requests: list[dict] = Field(default_factory=list)


class StoreSendRequest(BaseModel):
    """Release garments to line-stitching. Piece ids, not drawer ids."""
    piece_ids: list[uuid.UUID] = Field(default_factory=list)
    piece_barcodes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _not_empty(self):
        if not self.piece_ids and not self.piece_barcodes:
            raise ValueError("Select at least one garment to send.")
        return self


class StoreSendResult(BaseModel):
    count_sent: int = 0
    sent: list[str] = Field(default_factory=list)
    not_ready: list[dict] = Field(default_factory=list)
    not_found: list[str] = Field(default_factory=list)
    message: str


class StorePieceRow(BaseModel):
    piece_id: uuid.UUID
    piece_code: str
    store_state: str
    holding: str
    leather_in: bool
    lining_in: bool
    # THE ROLL-UP, not the whole answer. True means every accessory line this
    # style declares has been issued IN FULL against this garment. It cannot say
    # which line is missing — that is what the three fields below are for, and
    # what StorePieceDetail.accessories spells out line by line.
    accessories_in: bool
    kit_required: bool = False
    # NOT_REQUIRED (the style declares none) | PENDING | PARTIAL | ISSUED.
    # NOT_REQUIRED is not "nothing issued" — it tells the screen to hide the
    # accessory panel rather than render one that can never be satisfied.
    kit_status: str | None = None
    accessories_outstanding: float = 0.0
    needs_lining: bool
    # LEATHER / LINING / ACCESSORIES — what this garment is still owed, spelled
    # exactly as the scan response spells it so the two screens cannot disagree.
    awaiting: list[str] = Field(default_factory=list)
    complete: bool
    style_name: str | None = None
    colour: str | None = None
    size: str | None = None


class StorePieceDetail(StorePieceRow):
    """One garment WITH its accessory checklist, line by line.

    THIS IS THE ONE ENDPOINT THAT ANSWERS THE WHOLE ACCESSORY QUESTION — what the
    garment takes, what has been scanned, and what is still pending — without the
    caller subtracting anything:

      `accessories`          every line the SKU's recipe declares, each carrying
                             `qty_per_piece`, `issued_qty`, `outstanding`, a
                             `state` (ISSUED/PARTIAL/PENDING/UNRESOLVED) and a
                             human `label`
      `accessories_scanned`  the ones fully issued
      `accessories_pending`  what is still owed
      `accessories_progress` declared / scanned / pending counts and totals
      `pending_line`         the sentence: "Waiting for ZIP · YKK-60 BLACK."

    A garment is not "accessories: true"; it is five lines, each of which is or is
    not satisfied. All of it comes from the same `material_requirement_block` the
    scan response reads, so the two screens cannot disagree about one garment.
    """
    accessories: list[dict] = Field(default_factory=list)
    accessories_scanned: list[dict] = Field(default_factory=list)
    accessories_pending: list[dict] = Field(default_factory=list)
    accessories_progress: dict = Field(default_factory=dict)
    pending_line: str | None = None
    summary_line: str | None = None
    spec_confirmed: bool = False


class PieceMaterialLine(BaseModel):
    """One recipe line as it stands against ONE garment.

    Loose on purpose: the line payload is assembled by StyleSpecService and
    carries different keys for a leather line (which gains `consumed`) than for
    an accessory (which gains `issued_qty` / `outstanding`). Pinning every field
    here would be a second copy of that shape to keep in step.
    """
    model_config = ConfigDict(extra="allow")

    line_id: str | None = None
    scope: str | None = None            # STYLE | SKU
    category: str | None = None
    subtype: str | None = None
    article: str | None = None
    colour: str | None = None
    size: str | None = None             # the MATERIAL's size — a 60cm zip
    garment_size: str | None = None     # which GARMENTS it is for — an L jacket
    qty_per_piece: float | None = None
    uom: str | None = None
    # Only on a line that does NOT reach this garment:
    # other_sku | other_size | zeroed, plus a sentence saying which.
    reason: str | None = None
    reason_note: str | None = None


class PieceMaterialsApplies(BaseModel):
    """The recipe for THIS colourway at THIS size."""
    leather: PieceMaterialLine | None = None
    lining: PieceMaterialLine | None = None
    accessories: list[PieceMaterialLine] = Field(default_factory=list)


class PieceMaterialIssueRow(BaseModel):
    """One material actually issued to this garment — the ledger row."""
    model_config = ConfigDict(extra="allow")

    issue_id: str
    spec_line_id: str | None = None
    category: str | None = None
    subtype: str | None = None
    article: str | None = None
    colour: str | None = None
    qty: float
    uom: str | None = None
    material_lot_id: str | None = None
    # STORE_KIT (against a recipe line) | MANUAL (an off-spec correction, really
    # issued and deliberately not counted against the checklist).
    source: str | None = None
    issued_by_employee_id: str | None = None
    entered_by: str | None = None
    issued_at: datetime | None = None


class PieceMaterials(BaseModel):
    """EVERYTHING merged into one garment — and everything that is not.

    THE TWO HALVES ARE THE POINT. `applies` is this garment's own recipe;
    `not_applicable` is every other line on the style with the reason it does
    not reach here. Without the second half a style-wide requirement view and a
    per-garment kit scan look like they contradict each other: the view lists
    three accessories, the scan says there is nothing to issue, and both are
    telling the truth about different questions.
    """
    piece_id: str
    piece_code: str
    sku_id: str | None = None
    sku_label: str | None = None        # "NAVY · S" — what a person calls it
    style_id: str | None = None
    style_name: str | None = None
    garment_size: str | None = None
    colour: str | None = None

    spec_confirmed: bool = False
    no_accessories_declared: bool | None = None
    kit_required: bool = False
    kit_status: str | None = None
    summary_line: str | None = None

    applies: PieceMaterialsApplies = Field(default_factory=PieceMaterialsApplies)
    not_applicable: list[PieceMaterialLine] = Field(default_factory=list)
    issued: list[PieceMaterialIssueRow] = Field(default_factory=list)
    # The dcm recorded at the cut. It lives on the production EVENT, not in the
    # issue ledger (CLAUDE.md §8), and is merged here so a screen never has to
    # know there were two write paths.
    consumed: float | None = None
    store: dict | None = None
    needs_lining: bool | None = None
    lining_reason: str | None = None


class StoreList(BaseModel):
    """One page of the store. ADDITIVE on the old {count, pieces} shape.

    `count` is the rows in THIS page and keeps its old meaning; `total` is how
    many match the filter. A `limit` with no `offset` was a cap, not a pager —
    you could ask for the first 200 garments and had no way to ask for the next
    200 — so both are here now.
    """
    count: int
    total: int = 0
    limit: int = 200
    offset: int = 0
    has_more: bool = False
    pieces: list[StorePieceRow] = Field(default_factory=list)
