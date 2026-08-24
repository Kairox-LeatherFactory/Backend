"""modules/drawers/schemas.py — API contract for drawers & the merge gate."""
import uuid
from datetime import datetime

from pydantic import BaseModel, Field, model_validator


class StoreScanRequest(BaseModel):
    """Scan a part into its drawer.

    THE EMPLOYEE IS NOW REQUIRED (bug #2). The store scan used to accept no actor
    at all, so a drawer could be filled by nobody — the one door on the floor
    where "who did this" was unanswerable. The frontend was asked to make the
    employee scan mandatory; a rule the frontend alone enforces is a rule that
    disappears the moment anything else calls the API, so it is enforced here.

    `part` is now OPTIONAL (bug #18): the server infers LEATHER vs LINING from the
    piece's history and the drawer's contents. Send it only to override.
    """
    # Either code or id for each — barcode door sends codes, manual sends ids.
    employee_barcode: str | None = None
    employee_id: uuid.UUID | None = None
    drawer_barcode: str | None = None
    drawer_id: uuid.UUID | None = None
    piece_barcode: str | None = None
    piece_id: uuid.UUID | None = None
    # WIDENED, NEVER NARROWED: a client sending LEATHER or LINING is unaffected.
    # ACCESSORY is the store issuing the garment's accessory kit into the drawer,
    # and it is EXPLICIT-ONLY — the server never infers it, because a mis-inferred
    # kit would spend stock nobody asked to spend (see DrawerPart).
    part: str | None = Field(default=None, pattern="^(LEATHER|LINING|ACCESSORY)$")
    # Optional, ACCESSORY only. Omit to issue the full kit exactly as the style's
    # material spec says. Send it to issue part of the kit (the operator is short
    # of one article) or to substitute a different lot for one line.
    lines: list["KitLineRequest"] | None = None

    @model_validator(mode="after")
    def _actor_required(self):
        if not self.employee_barcode and not self.employee_id:
            raise ValueError(
                "Scan the employee barcode first — provide employee_barcode or "
                "employee_id.")
        return self


class KitLineRequest(BaseModel):
    """One line of a partial or substituted accessory issue."""
    spec_id: uuid.UUID
    qty: float | None = None            # default: everything still owed
    material_lot_id: uuid.UUID | None = None   # default: resolve from the spec


class KitLine(BaseModel):
    """One accessory line, as reported back on a scan."""
    spec_id: str
    category: str | None = None
    subtype: str | None = None
    article: str
    colour: str | None = None
    size: str | None = None
    qty_per_piece: float = 0
    qty: float = 0
    uom: str | None = None
    issued: float | None = None
    lot_id: str | None = None
    available_after: float | None = None
    # Set on `unresolved` lines only: NONE (no lot carries this article) or
    # AMBIGUOUS (several do, so a human must pick).
    reason: str | None = None
    candidate_lot_ids: list[str] = Field(default_factory=list)
    note: str | None = None


class KitBlock(BaseModel):
    """What this garment's accessories are, and what has happened to them.

    Returned on EVERY store scan, not only the accessory one — the operator
    holding the leather is the one who also has to find the buttons. On a cut-part
    scan it is a read-only checklist; on an accessory scan it is the receipt.
    """
    status: str                     # NOT_REQUIRED | PENDING | PARTIAL | ISSUED
    summary_line: str | None = None
    issued_now: list[KitLine] = Field(default_factory=list)
    already_issued: list[KitLine] = Field(default_factory=list)
    outstanding: list[dict] = Field(default_factory=list)
    unresolved: list[dict] = Field(default_factory=list)
    # Verbatim decrement warnings — one per line that went short. The issue was
    # still RECORDED; see MaterialService._decrement_nocommit.
    stock_warnings: list[dict] = Field(default_factory=list)
    complete: bool = False


class StoreScanResult(BaseModel):
    drawer_code: str
    piece_code: str
    state: str
    needs_lining: bool
    # Set when the effective lining requirement disagrees with the stored flag —
    # e.g. "its style name contains 'KNIT'". Display it verbatim beside `awaiting`.
    lining_reason: str | None = None
    awaiting: list[str]
    ready_for_received: bool
    part: str                       # the bucket used (LEATHER | LINING)
    part_inferred: bool = False     # True when the server chose it (bug #18)
    # The worker credited with the scan. Recorded as data on the audit row, never
    # as its actor — the actor is the login, and audit_log.actor_user_id is a
    # foreign key to app_user.
    employee_id: str | None = None
    holding: str = ""               # HOLDING LEATHER | HOLDING LINING | ...
    auto_received: bool = False     # completeness advanced it to RECEIVED (#13)
    # BUG #15 — scanning is NOT completion. The piece stays in the drawer until
    # someone selects it and sends it; these say so explicitly.
    sent: bool = False
    next_action: str = ""
    # ── the accessory kit ────────────────────────────────────────────────────
    kit: KitBlock | None = None
    # True when the kit was issued into a drawer that had already been RECEIVED.
    # Normal only where the style's spec arrived after its garments did.
    late_kit: bool = False



class DrawerTransition(BaseModel):
    transition: str = Field(pattern="^(RECEIVED|SENDED)$")


class DrawerTransitionResult(BaseModel):
    drawer_code: str
    piece_code: str | None
    state: str


class DrawerSendRequest(BaseModel):
    """Send one or many drawers onward in a single action.

    THERE IS NOTHING TO CHOOSE BUT THE DRAWERS. The store sits at one point in
    the pipeline: lining is cut and scanned INTO the drawer, so a merged drawer
    has exactly one way forward — LINE_STITCHING, then shell stitching, then
    final finish. Sending releases the pieces into that chain, which is what
    production reads through DrawerState.SENDED.
    """
    drawer_ids: list[uuid.UUID] = Field(min_length=1)


class DrawerSendResult(BaseModel):
    requested: int
    count_sent: int
    # PARTIAL ACCEPT, like the production gates: one drawer that is not ready
    # never loses the twenty that are. Each bucket carries its own reason.
    sent: list[dict] = Field(default_factory=list)
    not_ready: list[dict] = Field(default_factory=list)
    not_found: list[str] = Field(default_factory=list)
    pieces_released: list[str] = Field(default_factory=list)
    message: str = ""


class DrawerLabel(BaseModel):
    """One row of the Drawers List — printable AND choosable (bug #13).

    `barcode` is the string to encode on the label; `barcode_id` is the registry
    row that owns it. Both are null if the drawer has no registry row — that is a
    broken label the print sheet should SHOW, not silently drop.

    `state` is the drawer's LIFECYCLE position; `holding` is what is physically
    inside it. They are not the same question, and they stop agreeing the moment
    the drawer moves on: at state `received` or `sended` the lifecycle no longer
    says whether the drawer holds leather, lining or both, but `holding` still
    does."""
    drawer_id: uuid.UUID
    seq: int
    code: str
    state: str
    holding: str          # HOLDING LEATHER | HOLDING LINING | HOLDING BOTH | EMPTY
    leather_in: bool = False
    lining_in: bool = False
    # The third bucket. False on every drawer whose style declares no
    # accessories, so a list of pre-spec drawers renders exactly as it always has.
    accessories_in: bool = False
    kit_required: bool = False
    # The EFFECTIVE lining requirement, resolved from every signal — not the
    # stored piece.needs_lining flag, which is written once at upload and is
    # wrong for most of a live order. See core/lining_rules.py.
    needs_lining: bool = True
    lining_reason: str | None = None   # why, when it disagrees with the flag
    complete: bool = False
    piece_id: uuid.UUID | None = None
    piece_code: str | None = None
    piece_serial: str | None = None    # "001" (bug #7)
    can_send: bool = False             # drives the list's Send button
    barcode_id: uuid.UUID | None = None
    barcode: str | None = None
    caption: str | None = None
    barcode_status: str | None = None
    # ── WHY THIS ROW IS WHERE IT IS IN THE LIST ─────────────────────────────
    # `last_activity_at` is the sort key under the default `sort=recent`;
    # `last_activity` names the act that set it — "merged" | "scanned" |
    # "received" | "sent" | "released". Render them together ("sent · 2 min
    # ago"): a timestamp with no verb tells the operator when something
    # happened but not what, which is the half they need to decide whether to
    # open the drawer.
    last_activity_at: datetime | None = None
    last_activity: str | None = None
    # True when this row was floated up by `pin_codes` (the operator's own
    # recently-searched list) rather than by its activity time — the UI should
    # band these separately, or a pinned drawer from last week looks like the
    # busiest drawer on the floor.
    pinned: bool = False


class DrawerPoolGrow(BaseModel):
    """Add N permanent drawers to the pool. DM/MD only; one-way.

    Capped at 1000 per call — not a technical limit but a typo guard: `add: 20000`
    would mint 20,000 permanent barcoded drawers and there is no un-mint.
    """
    add: int = Field(ge=1, le=1000)


class DrawerLabelPage(BaseModel):
    total: int          # drawers matching the filters (ignores limit/offset)
    count: int          # rows in THIS page
    items: list[DrawerLabel]


class DrawerDetail(BaseModel):
    """One drawer, opened from the list (bug #13)."""
    drawer_id: uuid.UUID
    code: str
    seq: int
    state: str
    holding: str
    leather_in: bool
    lining_in: bool
    accessories_in: bool = False
    kit_required: bool = False
    needs_lining: bool           # EFFECTIVE requirement, not the stored flag
    lining_reason: str | None = None
    awaiting: list[str] = Field(default_factory=list)
    complete: bool
    received_at: datetime | None = None
    sended_at: datetime | None = None
    sent: bool = False
    can_send: bool = False
    # The full piece card: article, serial, order, style, colour, size.
    piece: dict | None = None
