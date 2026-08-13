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
    part: str | None = Field(default=None, pattern="^(LEATHER|LINING)$")

    @model_validator(mode="after")
    def _actor_required(self):
        if not self.employee_barcode and not self.employee_id:
            raise ValueError(
                "Scan the employee barcode first — provide employee_barcode or "
                "employee_id.")
        return self


class StoreScanResult(BaseModel):
    drawer_code: str
    piece_code: str
    state: str
    needs_lining: bool
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
    sent_to: str | None = None
    next_action: str = ""


class DrawerTransition(BaseModel):
    transition: str = Field(pattern="^(RECEIVED|SENDED)$")


class DrawerTransitionResult(BaseModel):
    drawer_code: str
    piece_code: str | None
    state: str


class DrawerSendRequest(BaseModel):
    """Send one or many drawers onward in a single action (bugs #13/#14).

    STITCHING is the destination that opens the merge gate — production reads
    DrawerState.SENDED to release a piece into LINE_STITCHING, so this call moves
    that whole bunch of pieces at once. LINING routes the drawer to the lining
    floor and leaves the gate shut.
    """
    drawer_ids: list[uuid.UUID] = Field(min_length=1)
    destination: str = Field(pattern="^(STITCHING|LINING)$")


class DrawerSendResult(BaseModel):
    destination: str
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
    complete: bool = False
    piece_id: uuid.UUID | None = None
    piece_code: str | None = None
    piece_serial: str | None = None    # "001" (bug #7)
    sent_to: str | None = None
    can_send: bool = False             # drives the list's Send button
    barcode_id: uuid.UUID | None = None
    barcode: str | None = None
    caption: str | None = None
    barcode_status: str | None = None


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
    needs_lining: bool
    awaiting: list[str] = Field(default_factory=list)
    complete: bool
    received_at: datetime | None = None
    sended_at: datetime | None = None
    sent_to: str | None = None
    sent: bool = False
    can_send: bool = False
    # The full piece card: article, serial, order, style, colour, size.
    piece: dict | None = None
