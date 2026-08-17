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


# ── order / style pickers ───────────────────────────────────────────────────
@router.get("/orders", response_model=list[schemas.OrderRateCard])
async def list_orders(
    on: date | None = Query(None, description="Coverage as of this date. Default today."),
    unpriced_only: bool = Query(False, description="Only orders with unpriced styles."),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_RATE_READERS),
):
    """ORDER CARDS — the payroll landing screen (change-list item 3).

    The screen is order → style → rate sheet. It used to open straight onto
    hundreds of style cards from every order at once, with nothing on the card
    saying which order it belonged to. Click an order here, then call
    GET /wages/styles?order_number=... for its style cards.

    `styles_priced / styles` is the card's badge, the same "n of m priced" idea as
    the style card one level up."""
    return await WageService(db).list_orders(on=on, unpriced_only=unpriced_only)


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
    """COMMAND. Computes payroll for the window the manager typed.

    THE RUN ENGINE. Two things it now accepts that it did not before:

      • `freeze` (default true). Send `false` to compute a DRAFT — an OPEN run you
        can recompute as often as you like, then freeze with
        POST /runs/{id}/close once the numbers are agreed. Recomputing a FROZEN
        run needs an explicit reopen; see POST /runs/{id}/reopen.
      • `order_number` / `style_code`. Narrow the run to one order or one style.
        A SCOPED RUN PAYS PIECE-RATE WORK ONLY (`piece_rate_only: true` on the
        response) — a monthly salary belongs to a person, not to a style, and
        emitting it on a one-style run would pay it again on the next one.

    Side-effecting and non-idempotent. Two runs over the same window for the same
    scope is a 409, because the same pieces would be paid twice. Check
    `unrated_operations` and `gap_days` before paying anyone.
    """
    return await WageService(db).compute_run(
        body.period_start, body.period_end, freeze=body.freeze,
        order_number=body.order_number, style_code=body.style_code)


@router.post("/runs/{run_id}/recompute", response_model=schemas.WageRunDetail)
async def recompute_run(
    run_id: uuid.UUID,
    body: schemas.RecomputeRequest | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(UserRole.DIRECT_MANAGER, UserRole.MANAGING_DIRECTOR)),
):
    """COMMAND. Discards a run's lines and rebuilds them from current production
    events and rates, for the SAME window and the SAME scope.

    FREE ON A DRAFT (status OPEN). On a CLOSED run this 409s and tells you to
    reopen it first — a frozen run is the document the cash was counted against,
    and unfreezing it should capture a reason. `confirm_closed: true` is the
    one-call escape hatch; it works, it stamps the recompute, and it records no
    reason, which is why the reopen door exists.

    The run keeps its id, window and scope; recompute_count increments and the
    actor is stamped, so a payslip reprinted afterwards is identifiably a
    different document from the one paid against.

    HR is deliberately NOT permitted: HR reads payroll, DM/MD authorise changes.
    """
    return await WageService(db).recompute_run(
        run_id, user_name=user.name,
        confirm_closed=bool(body and body.confirm_closed))


@router.post("/runs/{run_id}/reopen", response_model=schemas.WageRunReopenResult)
async def reopen_run(
    run_id: uuid.UUID,
    body: schemas.ReopenRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(
        UserRole.DIRECT_MANAGER, UserRole.MANAGING_DIRECTOR)),
):
    """COMMAND. UNFREEZE a CLOSED run so it can be recomputed. DM/MD only.

    The only door out of CLOSED, and deliberately a separate call from the
    recompute: a manager who must press reopen, type why, then press recompute
    cannot rewrite a paid payslip by mistyping a run id. The reason is stored and
    belongs on every reissued payslip for that period.
    """
    return await WageService(db).reopen_run(
        run_id, user_name=user.name, reason=body.reason)


@router.post("/runs/{run_id}/close", response_model=schemas.WageRunDetail)
async def close_run(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(
        UserRole.DIRECT_MANAGER, UserRole.MANAGING_DIRECTOR)),
):
    """COMMAND. FREEZE a draft run. From here it is the document of record.

    Idempotent — closing an already-closed run returns it unchanged, because
    that is what a double-click is and it changed nothing. Refuses to freeze a
    run with no lines: locking a window in which nobody is paid is never what
    was meant."""
    return await WageService(db).close_run(run_id, user_name=user.name)


@router.get("/runs/{run_id}/breakdown", response_model=schemas.RunBreakdown)
async def run_breakdown(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_PAYROLL_READERS),
):
    """QUERY. One frozen run folded three ways: per STYLE (with its stages and
    workers nested), per STAGE factory-wide, and per EMPLOYEE.

    This is the Run Engine result screen. All three folds come off the same
    frozen row set, so the three tabs cannot disagree with each other or with the
    run total — which they will if the frontend sums them itself."""
    return await WageService(db).run_breakdown(run_id)


@router.get("/runs/{run_id}/pieces", response_model=schemas.RunPiecePage)
async def run_pieces(
    run_id: uuid.UUID,
    style_code: str | None = Query(None, description="Narrow to one style."),
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_PAYROLL_READERS),
):
    """QUERY. PER-PIECE payroll detail: which garment, which stage, which worker,
    their EMPLOYEE BARCODE, and what that one piece paid.

    Amounts come from the run's FROZEN rate for that (employee, style, stage)
    cell — nothing is re-priced, so a rate corrected after the run closed does
    not change what this says the run paid. A row with `amount: null` carries a
    `note` explaining why (monthly worker, or the stage was unrated at run time);
    it is deliberately not a zero, which would read as work worth nothing."""
    return await WageService(db).run_pieces(
        run_id, style_code=style_code, limit=limit, offset=offset)


@router.get("/ledger", response_model=schemas.LedgerPage)
async def ledger(
    order_number: str | None = Query(None),
    style_code: str | None = Query(None),
    date_from: date | None = Query(None, description="Runs whose window ends on/after."),
    date_to: date | None = Query(None, description="Runs whose window starts on/before."),
    status: str | None = Query(None, description="open | closed"),
    limit: int = Query(10, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_PAYROLL_READERS),
):
    """QUERY. THE LEDGER — every computed run, LATEST COMPUTED FIRST, searchable
    by order, style, window and status.

    Ordered by when it was computed, not by its period: the question the ledger
    answers is "what did we just run". Each row carries `recompute_count` and
    `reopen_count` so a run that has been rebuilt or unfrozen since is visibly
    different from one that has not.

    Reads the FROZEN rows only. Nothing here is re-derived."""
    return await WageService(db).ledger(
        order_number=order_number, style_code=style_code, date_from=date_from,
        date_to=date_to, status=status, limit=limit, offset=offset)


@router.get("/runs/{run_id}", response_model=schemas.WageRunDetail)
async def get_run(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_PAYROLL_READERS),
):
    """QUERY. Re-reads a frozen run — payslip detail, no recomputation."""
    return await WageService(db).get_run_detail(run_id)
