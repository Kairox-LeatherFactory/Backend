"""
================================================================================
modules/wages/schemas.py — API contract for the wages module
================================================================================
NO UUIDs IN THE RATE SURFACE.
    Everything the rate screens touch is addressed by style_code / operation_code.
    The service resolves them to ids and 404s on a bad code — same contract as
    /production/scan taking sku_code. A manager can read a style code off a
    printed traveler; he cannot read a UUID off anything.

    Wage RUNS keep a uuid id, because a run has no natural key — there is nothing
    human-readable to name "the 1-15 April payroll" other than its window, and
    windows get re-typed and re-run. Runs are reached from GET /wages/runs, never
    typed by hand.

TWO RUN SHAPES, DELIBERATELY DIFFERENT.
    WageRunSummary   POST /runs — the confirmation screen. Totals and warnings.
    WageRunDetail    GET /runs/{id} — summary + every payslip line.
    POST is a command (freezes payroll, non-idempotent); GET is a query (re-reads
    a frozen snapshot). Not duplicates; the payloads make that visible.
================================================================================
"""
import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.enums import RunStatus


# ── style picker ────────────────────────────────────────────────────────────
class StyleRateOption(BaseModel):
    """One row of the wages landing screen. The manager clicks a code, not a UUID.

    rated_operations/total_operations drive the "3 of 7 priced" badge — an unpriced
    operation pays a worker nothing, so it must be visible BEFORE the run, not
    discovered in the unrated_operations warning afterwards.
    """

    style_code: str  # 'JP-CLERMONT_VEST'
    style_name: str
    article: str | None
    order_number: str
    sku_count: int
    qty_ordered: int
    rated_operations: int
    total_operations: int
    fully_rated: bool


# ── rates ───────────────────────────────────────────────────────────────────
class RateSet(BaseModel):
    """Single-cell rate save."""

    style_code: str
    operation_code: str
    rate: float = Field(ge=0)
    effective_from: date


class RateSetLine(BaseModel):
    operation_code: str
    rate: float = Field(ge=0)


class RateBulkSet(BaseModel):
    """One save of an edited rate sheet. All lines share style + effective_from."""

    style_code: str
    effective_from: date
    lines: list[RateSetLine] = Field(min_length=1)

    @model_validator(mode="after")
    def _no_duplicate_ops(self):
        seen = [ln.operation_code.strip().upper() for ln in self.lines]
        if len(seen) != len(set(seen)):
            raise ValueError("duplicate operation_code in lines")
        return self


class RateSheetRow(BaseModel):
    operation_code: str
    label: str
    sequence: int
    # null → no rate ever configured. Work logged here prices to nothing and the
    # worker earns zero for it. Render it red.
    rate: float | None = None
    effective_from: date | None = None


class RateSheetRead(BaseModel):
    style_code: str
    style_name: str
    order_number: str
    on: date
    operations: list[RateSheetRow]  # sequence order — CUTTING first
    missing_rate_count: int


class RateHistoryRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    rate: float
    effective_from: date


class RateHistoryRead(BaseModel):
    style_code: str
    operation_code: str
    history: list[RateHistoryRow]  # newest first


class OrderRateCard(BaseModel):
    """One order card on the payroll landing screen (change-list item 3).

    The drill is order → style → rate sheet. `styles_priced / styles` is the
    badge; `qty_ordered` is the "total piece" figure the card shows.
    """
    order_number: str
    styles: int
    styles_priced: int
    fully_priced: bool
    sku_count: int
    qty_ordered: int
    style_codes: list[str] = Field(default_factory=list)


# ── runs ────────────────────────────────────────────────────────────────────
class RunRequest(BaseModel):
    """The manager types both dates. Nothing is derived.

    `freeze=False` computes a DRAFT (status OPEN) the manager can recompute
    freely and then close. Default True preserves the shipped compute-and-freeze
    behaviour for every existing caller.

    `order_number` / `style_code` narrow the run. A SCOPED RUN PAYS PIECE-RATE
    WORK ONLY — a monthly salary is a fact about a person, not a style, so
    emitting it on a one-style run would pay it again on the next one.
    """

    period_start: date
    period_end: date
    freeze: bool = True
    order_number: str | None = None
    style_code: str | None = None

    @model_validator(mode="after")
    def _one_scope(self):
        if self.order_number and self.style_code:
            raise ValueError(
                "Scope a run by order_number OR style_code, not both — a style "
                "already belongs to exactly one order.")
        return self


class RecomputeRequest(BaseModel):
    """Optional body on POST /runs/{id}/recompute.

    `confirm_closed` is the one-call escape hatch for rewriting a FROZEN run. It
    works and it stamps the recompute, but it records NO REASON — which is why
    the intended route is POST /runs/{id}/reopen first.
    """
    confirm_closed: bool = False


class ReopenRequest(BaseModel):
    """Unfreeze a CLOSED run. The reason is not optional and not decorative — it
    is printed on every payslip reissued for this period."""
    reason: str = Field(min_length=5, max_length=500)


class WageRunReopenResult(BaseModel):
    id: uuid.UUID
    status: RunStatus
    period_start: date
    period_end: date
    reopen_count: int
    last_reopened_by: str | None = None
    last_reopen_reason: str | None = None
    message: str = ""


class UnratedOperation(BaseModel):
    """Real production that priced to nothing because no Rate row covered it.

    Codes, not ids, so the frontend can link the warning straight through to
    GET /wages/rate-sheet?style_code=... and the manager fixes it in one click.
    """

    style_code: str
    style_name: str
    operation_code: str
    unpaid_pieces: int


class WageLineBreakdown(BaseModel):
    """One (style x operation) contribution to an employee's payroll line."""
    style_code: str
    style_name: str
    operation_code: str
    operation_label: str
    pieces: int
    rate: float          # rupees per piece applied to these pieces
    amount: float


class WageLineDetail(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    employee_name: str
    designation: str | None
    wage_type: str
    pieces: int
    amount: float
    # Null when the employee worked more than one style/operation — read
    # `breakdown` instead of averaging, which would be a number nobody was paid.
    rate: float | None = None
    style_codes: list[str] = Field(default_factory=list)
    breakdown: list[WageLineBreakdown] = Field(default_factory=list)


class WageRunSummary(BaseModel):
    id: uuid.UUID
    period_start: date
    period_end: date
    status: RunStatus
    # NULL = the whole factory. Set when the run was narrowed (change-list item 3).
    scope_order_number: str | None = None
    scope_style_code: str | None = None
    # True when the monthly branch was skipped because the run is scoped. The
    # screen must say "piece-rate only" rather than let a manager read a
    # one-style run as a full payroll.
    piece_rate_only: bool = False
    total_amount: float
    total_pieces: int
    employee_count: int
    unrated_operations: list[UnratedOperation] = Field(default_factory=list)
    lines: list[WageLineDetail] = Field(default_factory=list)
    gap_days: int = 0
    recomputed: bool = False
    recompute_count: int = 0


class WageRunDetail(WageRunSummary):
    lines: list[WageLineDetail]
    last_recomputed_at: datetime | None = None
    last_recomputed_by: str | None = None
    reopen_count: int = 0
    last_reopened_at: datetime | None = None
    last_reopened_by: str | None = None
    last_reopen_reason: str | None = None


# ── the Run Engine result screen ────────────────────────────────────────────
class StageAmount(BaseModel):
    operation_code: str
    operation_label: str | None = None
    sequence: int = 0
    pieces: int
    amount: float
    rate: float | None = None


class EmployeeAmount(BaseModel):
    employee_id: uuid.UUID
    employee_name: str
    designation: str | None = None
    pieces: int
    amount: float
    styles: list[str] = Field(default_factory=list)


class StyleAmount(BaseModel):
    """One style's total, with its stages and its workers nested inside it.

    Nested rather than three sibling lists because that is the question the
    screen asks: "this style cost X; here is where it went". Flat lists would
    make the frontend re-join them, which is where the tabs start disagreeing.
    """
    style_code: str | None = None
    style_name: str | None = None
    pieces: int
    amount: float
    stages: list[StageAmount] = Field(default_factory=list)
    employees: list[EmployeeAmount] = Field(default_factory=list)


class RunBreakdown(BaseModel):
    run_id: uuid.UUID
    period_start: date
    period_end: date
    status: RunStatus
    scope_order_number: str | None = None
    scope_style_code: str | None = None
    computed_at: datetime | None = None
    recompute_count: int = 0
    reopen_count: int = 0
    total_amount: float
    total_pieces: int
    by_style: list[StyleAmount] = Field(default_factory=list)
    by_stage: list[StageAmount] = Field(default_factory=list)
    by_employee: list[EmployeeAmount] = Field(default_factory=list)


class RunPieceRow(BaseModel):
    """One garment, at one stage, and what it paid the worker who did it."""
    piece_code: str
    serial: str | None = None
    colour: str | None = None
    size: str | None = None
    style_code: str | None = None
    style_name: str | None = None
    operation_code: str
    operation_label: str | None = None
    employee_id: uuid.UUID
    employee_name: str
    designation: str | None = None
    # The card the scanner gun reads — asked for explicitly beside the name.
    employee_barcode: str | None = None
    work_date: date
    qty: int = 1
    # NULL (never 0) when this cell was not priced in the run — see `note`.
    rate: float | None = None
    amount: float | None = None
    note: str | None = None


class RunPiecePage(BaseModel):
    run_id: uuid.UUID
    status: RunStatus
    total: int
    count: int
    items: list[RunPieceRow] = Field(default_factory=list)


class LedgerRow(BaseModel):
    run_id: uuid.UUID
    period_start: date
    period_end: date
    status: RunStatus
    scope_order_number: str | None = None
    scope_style_code: str | None = None
    computed_at: datetime | None = None
    recompute_count: int = 0
    last_recomputed_at: datetime | None = None
    reopen_count: int = 0
    total_amount: float
    total_pieces: int
    employee_count: int


class LedgerPage(BaseModel):
    count: int
    items: list[LedgerRow] = Field(default_factory=list)