import uuid
from datetime import date

from pydantic import BaseModel, ConfigDict, Field, model_validator


class OperationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    code: str
    label: str
    sequence: int


# ---- SKU picker (friendly name + stored code, no UUID on screen) -----------
class SkuOption(BaseModel):
    sku_id: uuid.UUID
    code: str | None          # "57-M" — unique per style, readable
    label: str                # "CLERMONT · PINE GREEN · M"
    order_number: str | None
    style_name: str | None
    color_code: str | None
    color_name: str | None
    size: str | None
    qty_ordered: int


# ---- Pieces ----------------------------------------------------------------
class PieceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    code: str                 # "KJ2451-CLERMONT-57-M-005"
    seq: int                  # 5
    sku_id: uuid.UUID
    current_operation_id: uuid.UUID | None


class CuttingCreate(BaseModel):
    """Mint N pieces for a SKU at the cutting table."""
    sku_id: uuid.UUID | None = None
    sku_code: str | None = None
    employee_id: uuid.UUID
    work_date: date
    count: int = Field(gt=0)
    
    @model_validator(mode="after")
    def _one_sku(self):
        if not self.sku_id and not self.sku_code:
            raise ValueError("Provide sku_id or sku_code.")
        return self


class CuttingResult(BaseModel):
    sku_id: uuid.UUID
    count: int
    pieces: list[PieceRead]   # print these codes on the traveler cards


class ScanBatchCreate(BaseModel):
    """Log a batch at one stage. Give EITHER sku_id+piece_seqs OR piece_codes."""
    operation_id: uuid.UUID
    employee_id: uuid.UUID
    work_date: date
    sku_id: uuid.UUID | None = None
    sku_code: str | None = None 
    piece_seqs: list[int] | None = None       # [1, 2, 5, 7] within sku_id
    piece_codes: list[str] | None = None      # full codes (typed / scanned)

    @model_validator(mode="after")
    def _one_input(self):
        has_sku = bool(self.sku_id or self.sku_code)         # <-- sku_code counts
        if not self.piece_codes and not (has_sku and self.piece_seqs):
            raise ValueError("Provide (sku_id + piece_seqs) or piece_codes.")
        return self


class ScanBatchResult(BaseModel):
    operation: str
    count_logged: int
    logged: list[str]         # piece codes logged
    rework: list[str]         # pieces already seen at this stage (looped back)
    not_found: list[str]      # unknown seqs/codes — skipped, never written


class PieceOption(BaseModel):
    piece_id: uuid.UUID
    code: str                        # "KJ2451-CLERMONT-57-M-005" — show this
    seq: int                         # 5 — what /scan takes in piece_seqs
    current_stage: str | None        # "CUTTING"; null if never logged
    current_stage_label: str | None
    done_at_op: bool = False         # already logged at the requested operation


class SkuPieceList(BaseModel):
    sku_id: uuid.UUID
    sku_code: str | None
    order_id: uuid.UUID | None
    colour: str | None
    size: str | None
    operation_id: uuid.UUID | None
    operation_code: str | None
    total: int
    done: int
    pending: int
    pieces: list[PieceOption]

# ---- Legacy generic event --------------------------------------------------
class ProductionEventCreate(BaseModel):
    sku_id: uuid.UUID
    operation_id: uuid.UUID
    employee_id: uuid.UUID
    work_date: date
    qty: int = Field(gt=0)


class ProductionEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    sku_id: uuid.UUID
    operation_id: uuid.UUID
    employee_id: uuid.UUID
    work_date: date
    qty: int
    entered_by: str | None
    piece_id: uuid.UUID | None


class StyleStageProgress(BaseModel):
    """The live status of one style across all its operations."""
    style_id: uuid.UUID
    style_name: str
    qty_ordered: int
    stages: dict[str, int]    # {"CUTTING": 152, "FUSING": 152, "PASTING": 150, ...}
    
class ScanResult(BaseModel):
    operation: str
    count_logged: int
    logged: list[str] = Field(default_factory=list)
    rework: list[str] = Field(default_factory=list)
    not_found: list[str] = Field(default_factory=list)
    sequence_blocked: list[str] = Field(default_factory=list)
    skill_blocked: list[str] = Field(default_factory=list)
    
class Actor(BaseModel):
    employee_barcode: str | None = None
    employee_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _one(self):
        if not self.employee_barcode and not self.employee_id:
            raise ValueError("Provide employee_barcode or employee_id.")
        return self


class Targets(BaseModel):
    piece_barcodes: list[str] | None = None
    sku_id: uuid.UUID | None = None
    piece_seqs: list[int] | None = None

    @model_validator(mode="after")
    def _one(self):
        if not self.piece_barcodes and not (self.sku_id and self.piece_seqs):
            raise ValueError("Provide piece_barcodes OR (sku_id + piece_seqs).")
        return self


class Consumption(BaseModel):
    """What the cut consumed — reachable through EITHER door.

    ID DOOR (unchanged): the screen already picked a lot, so it sends the id.
    SPEC DOOR (bugs #9/#10): the cutting manager types what is on the hide —
    article, colour, and optionally a thickness — and the router resolves it to a
    lot through the existing /materials/lots picker.

    THICKNESS IS OPTIONAL AND FREE TEXT, deliberately. It was a required dropdown,
    which meant a hide whose thickness was not already in the list could not be
    logged at all; the floor's answer to that is to pick a wrong value, which is
    worse than a blank. It narrows the lot match when supplied and is ignored when
    not.
    """
    leather_lot_id: uuid.UUID | None = None
    lining_lot_id: uuid.UUID | None = None
    dcm: float | None = None
    article: str | None = None
    colour: str | None = None
    thickness: str | None = None


class LogRequest(BaseModel):
    screen_context: str | None = None       # (item 5) role-derived if omitted
    actor: Actor
    targets: Targets
    work_date: date
    consumption: Consumption | None = None
    preview: bool = False                    # NEW: dry-run, compute buckets, write nothing


class LogResult(BaseModel):
    # A cut screen fixes one stage. PIPELINE infers a stage PER PIECE, so a batch
    # can span several: `stage` is then "MIXED" and `stage_by_piece` carries the
    # truth. `stage` is null ONLY when no piece resolved to a loggable stage at
    # all — `message` and `blocked` say why.
    stage: str | None
    stages: list[str] = Field(default_factory=list)
    stage_by_piece: dict[str, str] = Field(default_factory=dict)
    count_logged: int
    logged: list[str]
    rework: list[str]
    not_found: list[str]                     # codes/ids that did not resolve
    completed: list[str] = Field(default_factory=list)   # past the final stage
    sequence_blocked: list[str]
    skill_blocked: list[str]
    merge_blocked: list[str]
    # Pieces whose inferred stage this role may not log, in a MIXED batch where
    # other stages WERE permitted. An all-denied batch is still a 403.
    role_blocked: list[str] = Field(default_factory=list)
    # Every per-piece rejection WITH its cause: {piece, stage, gate, reason}.
    # gate ∈ {role, sequence, merge, not_cut, completed}. The flat lists above
    # carry the same pieces but no reason, and are kept for existing callers.
    blocked: list[dict] = Field(default_factory=list)
    message: str = ""                        # one sentence for the scan screen
    screen_role_warning: str | None = None
    # Set when a cut consumed more than the lot had available. The cut IS still
    # recorded — stock drifts and the garment is physically cut — but the
    # shortfall is surfaced instead of silently driving on_hand negative.
    # {lot_id, article, colour, uom, requested, available_before, short_by,
    #  on_hand_after, note}
    stock_warning: dict | None = None
    consumption_recorded: dict | None = None
    preview: bool = False                    # NEW: echoes whether this was a dry-run
    # GATE 2 is a WARNING, not a block: the piece is logged and the anomaly is
    # reported here ({piece, employee, designation, stage, note}). The service
    # has always returned these; without the field the response model dropped
    # them, so the floor never saw the warning it was told it would get.
    skill_warnings: list[dict] = Field(default_factory=list)
    # BUG #12 — {piece_code: {drawer_id, code, state, holding, leather_in,
    # lining_in, sent_to}} for every scanned piece that has a drawer.
    drawer_by_piece: dict[str, dict] = Field(default_factory=dict)
    # BUG #8 — {sku_id, stage, total, done, remaining, closed} for the stage just
    # logged. `closed: true` means every piece of the SKU is done here and the
    # style must stop being offered for scanning. Null on a preview or a MIXED
    # batch (there is no single stage to report against).
    sku_progress: dict | None = None


# ── the scan-time state read (bugs #4, #6, #8, #12) ─────────────────────────
class PieceStageCard(BaseModel):
    """One stage card, and whether the operator may scan into it.

    state ∈ completed | next | locked | not_applicable
      completed       an event exists at this stage
      next            the stage this scan would log
      locked          a gate refuses it — `gate` + `reason` say which and why
      not_applicable  off this garment's route (a lining cut on an unlined piece)
    """
    stage: str
    state: str
    gate: str | None = None          # sequence | merge
    reason: str | None = None
    requires_consumption: bool = False


class PieceState(BaseModel):
    """ONE PIECE — its identity, where it is, where it goes next, and whether the
    scan can just be logged.

    This is what a barcode scan should return. `piece` is the individual garment
    (code, serial, article, style, colour, size), never a SKU roll-up: a SKU
    describes a whole style and cannot answer anything about the item in the
    operator's hand.
    """
    piece: dict                      # the full barcode piece card (article, serial…)
    drawer: dict | None = None       # bug #12
    completed_stages: list[str] = Field(default_factory=list)
    # WHERE THE PIECE IS NOW — the real, event-backed stage. Distinct from
    # `display_stage`, which may read STORE (a drawer state, not an event).
    current_stage: str | None = None
    current_stage_label: str | None = None
    next_stage: str | None = None    # bug #4 — inferred, never chosen by the user
    next_stage_requires_consumption: bool = False
    display_stage: str | None = None  # may be the virtual STORE
    display_label: str | None = None
    in_store: bool = False
    stages: list[PieceStageCard] = Field(default_factory=list)   # bug #6
    sku: dict                        # bug #8 — total/done/remaining/closed
    # Whether THIS login's role could log next_stage. Lets the screen say "ask the
    # stitching manager" instead of letting the scan come back 403.
    can_log_next: bool | None = None
    # The scanned worker, when one was supplied: name, designation, whether they
    # are checked in today, and any skill anomaly. Skill is a WARNING — the log
    # records the piece and reports it — so it never appears in `blockers`.
    actor: dict | None = None
    # Everything standing between this scan and a logged event. Each entry is
    # {gate, reason}; gate ∈ {employee, attendance, role, sequence, merge,
    # consumption, completed}.
    blockers: list[dict] = Field(default_factory=list)
    # THE ONE BOOLEAN AN AUTOMATIC SCREEN NEEDS.
    #   true  → POST /production/log now; no stage to pick, nothing to ask
    #   false → show `blockers`
    #   null  → no employee was supplied, so the question is unanswered (which is
    #           not the same as "blocked", and must not be rendered as one)
    ready_to_log: bool | None = None