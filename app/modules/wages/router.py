"""
================================================================================
modules/wages/router.py — Wages HTTP API (async)
================================================================================
THE FRONTEND FLOW
    GET  /wages/styles                       landing screen — readable style codes
                                             with a "3 of 7 priced" badge
    GET  /wages/rate-sheet?style_code=...    click a code -> the rate table
    POST /wages/rates/bulk                   save the edited column
    GET  /wages/rate-history?style_code=...&operation_code=...
    POST /wages/runs                         payroll for a hand-typed window
    GET  /wages/runs, /wages/runs/{id}       history and payslips

    No UUID is ever shown or typed on the rate screens. style_code and
    operation_code go in; the service resolves them. Same contract as
    /production/scan, which takes sku_code.

AUTH NOTE — reads here are role-gated, not merely authenticated.
    Every other read endpoint in this codebase uses bare get_current_user. These
    do not, because CLIENT is a real user role with a real login, and per-piece
    labour rates are the factory's cost structure. A client who can see that
    CUTTING pays 12.50/pc on their own style is holding your margin during the
    next price negotiation. Worth auditing the other modules for the same leak.

ROUTE ORDER IS LOAD-BEARING.
    Static segments must be declared before /runs/{run_id}. FastAPI matches
    top-down and would otherwise try to parse a literal path segment as a UUID and
    422. Style codes are QUERY parameters, not path segments, for the same class of
    reason: a code like 'JP-CLERMONT_VEST' is fine in a path, but the day someone
    imports a style whose name slugs to something with a slash in it, a path
    parameter breaks and a query parameter does not.
================================================================================
"""
import uuid
from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.enums import UserRole
from app.modules.users.deps import require_roles
from app.modules.users.models import User
from app.modules.wages import schemas
from app.modules.wages.service import WageService

router = APIRouter(prefix="/wages", tags=["Wages"])

# Who may look at labour costs at all.
_RATE_READERS = require_roles(
    UserRole.DIRECT_MANAGER, UserRole.MANAGING_DIRECTOR, UserRole.HR
)
# Who may look at payroll.
_PAYROLL_READERS = require_roles(UserRole.DIRECT_MANAGER, UserRole.MANAGING_DIRECTOR, UserRole.HR)


# ── style picker ────────────────────────────────────────────────────────────
@router.get("/styles", response_model=list[schemas.StyleRateOption])
async def list_styles(
    order_number: str | None = Query(None, description="Filter to one order."),
    client_id: uuid.UUID | None = Query(None, description="Filter to one client."),
    unpriced_only: bool = Query(
        False, description="Only styles with at least one unpriced operation."
    ),
    on: date | None = Query(None, description="Coverage as of this date. Default today."),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_RATE_READERS),
):
    """The wages landing screen. Call this first — it is what the manager clicks.

    Returns style codes ('JP-CLERMONT_VEST'), never style ids. Each row carries
    rated_operations/total_operations so unpriced styles are visible before payroll
    runs and silently pays zero for them.
    """
    return await WageService(db).list_styles(
        order_number=order_number,
        client_id=client_id,
        unpriced_only=unpriced_only,
        on=on,
    )


# ── rates ───────────────────────────────────────────────────────────────────
@router.get("/rate-sheet", response_model=schemas.RateSheetRead)
async def rate_sheet(
    style_code: str = Query(..., description="e.g. JP-CLERMONT_VEST"),
    on: date | None = Query(None, description="Rates in force on this date. Default today."),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_RATE_READERS),
):
    """Every operation of a style with its current rate. The rate-setting screen."""
    return await WageService(db).rate_sheet(style_code, on or date.today())


@router.get("/rate-history", response_model=schemas.RateHistoryRead)
async def rate_history(
    style_code: str = Query(..., description="e.g. JP-CLERMONT_VEST"),
    operation_code: str = Query(..., description="e.g. CUTTING"),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_RATE_READERS),
):
    """Every rate ever set for one style x operation, newest first."""
    return await WageService(db).rate_history(style_code, operation_code)


@router.post("/rates")
async def set_rate(
    body: schemas.RateSet,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.DIRECT_MANAGER)),
):
    """Single-cell save. Prefer /rates/bulk when saving a whole sheet."""
    return await WageService(db).set_rate(body)


@router.post("/rates/bulk")
async def set_rates_bulk(
    body: schemas.RateBulkSet,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.DIRECT_MANAGER)),
):
    """Save an edited rate sheet in one transaction. All codes resolved first."""
    return await WageService(db).set_rates_bulk(body)


# ── runs ────────────────────────────────────────────────────────────────────
@router.get("/runs", response_model=list[schemas.WageRunSummary])
async def list_runs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_PAYROLL_READERS),
):
    """Run history, newest first. Without this a run_id is unreachable once the tab
    that created it is closed."""
    return await WageService(db).list_runs(limit, offset)


@router.post("/runs", response_model=schemas.WageRunSummary, status_code=201)
async def compute_run(
    body: schemas.RunRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.DIRECT_MANAGER)),
):
    """COMMAND. Computes and freezes payroll for the window the manager typed.

    Side-effecting and non-idempotent — firing it twice on overlapping windows is a
    payroll incident, which is why the service returns 409 rather than allowing it.
    Response is the confirmation summary; check unrated_operations and gap_days
    before paying anyone.
    """
    return await WageService(db).compute_run(body.period_start, body.period_end)


@router.get("/runs/{run_id}", response_model=schemas.WageRunDetail)
async def get_run(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_PAYROLL_READERS),
):
    """QUERY. Re-reads a frozen run — payslip detail, no recomputation."""
    return await WageService(db).get_run_detail(run_id)
