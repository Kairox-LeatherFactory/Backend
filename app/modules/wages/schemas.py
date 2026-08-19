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

from app.core.enums import RunStatus, WageRunKind


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
    """The manager types both dates AND says which payroll this is.

    `run_kind` IS THE FIRST DECISION, and the two kinds are priced by
    incompatible rules (see core.enums.WageRunKind):

      piece    (DEFAULT) Pays PIECE_RATE workers. **REQUIRES order_number or
               style_code.** A piece wage is earned on a specific garment, so
               naming that garment's style is what the run IS, not a filter on
               it. Dates alone used to be accepted here, and that is how a
               manager pays the whole factory's piece work under a window they
               meant to narrow.

      monthly  Pays salaried staff, priced by the calendar alone. Needs no
               scope — but it MAY carry an order_number / style_code purely as a
               LABEL for the payroll screen ("the month we ran CLERMONT"). The
               label is stored and displayed; it changes nothing about who is
               paid or how much, and the overlap guard ignores it, so two
               monthly runs over one window still collide no matter how they are
               labelled.

    A piece run and a monthly run over the SAME window do not conflict — their
    employee populations are disjoint, and together they are one full payroll.

    `freeze=False` computes a DRAFT (status OPEN) the manager can recompute
    freely and then close.
    """

    period_start: date
    period_end: date
    freeze: bool = True
    run_kind: WageRunKind = WageRunKind.PIECE
    order_number: str | None = None
    style_code: str | None = None

    @model_validator(mode="after")
    def _scope_rules(self):
        if self.order_number and self.style_code:
            raise ValueError(
                "Scope a run by order_number OR style_code, not both — a style "
                "already belongs to exactly one order.")
        if self.run_kind is WageRunKind.COMBINED:
            raise ValueError(
                "run_kind 'combined' marks runs computed before piece and "
                "monthly payroll were split; it cannot be created. Compute a "
                "'piece' run (naming an order or style) and a 'monthly' run for "
                "the same window instead.")
        # THE RESTRICTION, enforced at the schema so it is visible in the OpenAPI
        # contract and the frontend can grey out the compute button before the
        # round trip. The service repeats it, because a rule only the HTTP layer
        # knows is a rule that vanishes the moment anything else calls compute.
        if self.run_kind is WageRunKind.PIECE and not (
                self.order_number or self.style_code):
            raise ValueError(
                "A piece-rate run must name the work it pays for: send "
                "`style_code` or `order_number`. Two dates alone would pay every "
                "piece of every order in that window. To pay salaries instead, "
                "send run_kind='monthly'.")
        return self


class RecomputeRequest(BaseModel):
    """Optional body on POST /runs/{id}/recompute.

    `confirm_closed` is the one-call escape hatch for rewriting a FROZEN run. It
    works and it stamps the recompute, but it records NO REASON — which is why
    the intended route is POST /runs/{id}/reopen first.
    """
    confirm_closed: bool = False


class DeleteRunRequest(BaseModel):
    """Optional body on DELETE /wages/runs/{id}.

    `confirm_closed` is required to delete a FROZEN run, because that run is the
    document its payments were counted against — removing it destroys the only
    record of money that already left the building. The ordinary case (clearing
    an OPEN draft, or wreckage from a compute that died halfway and left a
    committed run occupying the window) needs no flag."""
    confirm_closed: bool = False


class DeleteRunResult(BaseModel):
    id: uuid.UUID
    deleted: bool = True
    period_start: date
    period_end: date
    status: RunStatus
    run_kind: WageRunKind | None = None
    scope_order_number: str | None = None
    scope_style_code: str | None = None
    lines_deleted: int = 0
    message: str = ""


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
    # WHICH payroll this run is. 'combined' means it pre-dates the split and paid
    # both populations from one window.
    run_kind: WageRunKind = WageRunKind.COMBINED
    # The order / style this run is ABOUT. On a piece run these narrow what was
    # paid; on a monthly run they are a label (see scope_is_label). NULL on
    # neither = the whole factory.
    scope_order_number: str | None = None
    scope_style_code: str | None = None
    # TRUE when the two fields above are DECORATION, not a narrowing — i.e. on a
    # monthly run. The screen must not print "CLERMONT payroll" over a sheet
    # that paid every salaried person in the building.
    scope_is_label: bool = False
    # This run pays piece work only — say so, rather than let a manager read a
    # one-style run as a full payroll.
    piece_rate_only: bool = False
    # …and its mirror: a salaries-only sheet with no piece work on it.
    monthly_only: bool = False
    total_amount: float
    total_pieces: int
    employee_count: int
    unrated_operations: list[UnratedOperation] = Field(default_factory=list)
    lines: list[WageLineDetail] = Field(default_factory=list)
    gap_days: int = 0
    recomputed: bool = False
    recompute_count: int = 0
    # Present on list rows so the run history can be sorted and audited without
    # opening each run. NULL on a freshly computed payload.
    reopen_count: int = 0
    computed_at: datetime | None = None


class WageRunDetail(WageRunSummary):
    lines: list[WageLineDetail]
    last_recomputed_at: datetime | None = None
    last_recomputed_by: str | None = None
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