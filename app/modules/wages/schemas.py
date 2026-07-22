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
from datetime import date

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


# ── runs ────────────────────────────────────────────────────────────────────
class RunRequest(BaseModel):
    """The manager types both dates. Nothing is derived."""

    period_start: date
    period_end: date


class UnratedOperation(BaseModel):
    """Real production that priced to nothing because no Rate row covered it.

    Codes, not ids, so the frontend can link the warning straight through to
    GET /wages/rate-sheet?style_code=... and the manager fixes it in one click.
    """

    style_code: str
    style_name: str
    operation_code: str
    unpaid_pieces: int


class WageLineDetail(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    employee_name: str
    designation: str | None
    wage_type: str
    pieces: int
    amount: float


class WageRunSummary(BaseModel):
    id: uuid.UUID
    period_start: date
    period_end: date
    status: RunStatus
    total_amount: float
    total_pieces: int
    employee_count: int
    unrated_operations: list[UnratedOperation] = Field(default_factory=list)
    lines: list[WageLineDetail] = Field(default_factory=list)
    # Days between the last closed run's end and this run's start. Non-zero means a
    # stretch of work was never covered by any payroll. Informational — hand-typed
    # periods make gaps possible and nothing else would catch them.
    gap_days: int = 0


class WageRunDetail(WageRunSummary):
    lines: list[WageLineDetail]
