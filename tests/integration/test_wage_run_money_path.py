"""
INTEGRATION · the payroll money path, router-free, against a real DB.

Priority-1 coverage. Each test pins one invariant the business stated:

    · one line per employee per run          (uq_wage_line_run_emp)
    · PIECE_RATE and MONTHLY are exclusive   (never both for one worker)
    · a closed run is a frozen snapshot      (never silently recomputed)
    · windows may not overlap                (the same pieces paid twice)
    · rates are date-effective               (mid-period change splits by day)

Several of these currently sit behind AUDIT F139, which kills the piece-rate
aggregate before any of them is reached. Those tests are xfail'd against the
finding rather than worked around: the audit rule is that application code is
never edited to make a test pass. Each one flips to XPASS when F139 lands.
"""
from datetime import date, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.core.enums import RunStatus, WageType
from app.modules.clients import models as cm
from app.modules.employees import models as em
from app.modules.production import models as pm
from app.modules.wages.models import Rate, WageLine, WageRun
from app.modules.wages.service import WageService

F139 = pytest.mark.xfail(
    reason="AUDIT F139 (BLOCKER): production/repository.py:213,222 GROUP/ORDER BY "
           "Piece.style_id, which does not exist — every piece-rate run raises "
           "AttributeError before emitting SQL. Not fixed here by design.",
    raises=AttributeError, strict=False)

# The window must end on or before today (_validate_window, service.py:291-295).
END = date.today() - timedelta(days=1)
START = END - timedelta(days=13)          # a 14-day fortnight
WORK_DAY = START + timedelta(days=2)


async def _world(db, *, salary=30000):
    """One style, one operation, one piece-rate cutter, one monthly tailor."""
    client = cm.Client(name="MoneyCo"); db.add(client); await db.flush()
    po = cm.ClientOrder(client_id=client.id, order_number="MONEY-PO")
    db.add(po); await db.flush()
    style = cm.Style(client_order_id=po.id, name="CARNABY", code="CARNABY", production_status="RELEASED")
    db.add(style); await db.flush()
    sku = cm.SKU(style_id=style.id, color_code="57", size="M", qty_ordered=100,
                 code="MONEY-PO-CARNABY-57-M")
    db.add(sku); await db.flush()

    op = pm.Operation(code="LEATHER_CUTTING", label="Leather Cutting", sequence=1)
    db.add(op); await db.flush()

    cutter = em.Employee(name="PIECEWORKER", designation="CUTTER",
                         wage_type=WageType.PIECE_RATE, is_active=True)
    monthly = em.Employee(name="SALARIED", designation="TAILOR",
                          wage_type=WageType.MONTHLY, monthly_salary=salary,
                          is_active=True)
    db.add_all([cutter, monthly])
    await db.commit()
    for o in (client, po, style, sku, op, cutter, monthly):
        await db.refresh(o)
    return dict(style=style, sku=sku, op=op, cutter=cutter, monthly=monthly)


# ── THE PIECE / MONTHLY FORK ────────────────────────────────────────────────
# compute_run no longer computes "the payroll" — it computes ONE of two, and a
# PIECE run must name the work it pays for (a window with no style behind it
# pays every garment in those dates). Every call below goes through one of these
# two helpers so the contract is stated once.
async def _piece_run(db, w=None, **kw):
    """A piece-rate run, scoped to the world's one style."""
    kw.setdefault("style_code", "CARNABY")
    return await WageService(db).compute_run(run_kind="piece", **kw)


async def _monthly_run(db, **kw):
    """A salaries run. Takes no paying scope; an order/style here is a label."""
    return await WageService(db).compute_run(run_kind="monthly", **kw)


def _line_for(payload, name):
    """Find a worker's line by NAME.

    Deliberately not by id: the service returns `employee_id` as a raw UUID object
    in the service-level payload (it is only coerced to a string when the router
    serialises through `WageRunDetail`), so an id comparison here silently matches
    nothing. Name is unique per employee (employees/service.py disambiguates
    collisions with an IN-CHAL prefix), so it is the stable key at this layer.
    """
    return next(l for l in payload["lines"] if l["employee_name"] == name)


async def _log(db, w, *, qty, on: date, employee=None):
    db.add(pm.ProductionEvent(
        sku_id=w["sku"].id, operation_id=w["op"].id,
        employee_id=(employee or w["cutter"]).id, work_date=on, qty=qty,
        entered_by="test"))
    await db.commit()


async def _rate(db, w, *, value, effective_from):
    db.add(Rate(style_id=w["style"].id, operation_id=w["op"].id,
                rate=value, effective_from=effective_from))
    await db.commit()


# ══════════════════════════════════════════════ one line per employee per run
@pytest.mark.asyncio
async def test_wage_line_uniqueness_is_enforced_by_the_database(db):
    """The constraint, not the service, is the guarantee. F21/F102 added
    `uq_wage_line_run_emp` in wages/models.py:107 AND the migration
    (20260731_wage_line_uniq.py:30) — this asserts it actually bites."""
    from sqlalchemy.exc import IntegrityError

    w = await _world(db)
    run = WageRun(period_start=START, period_end=END, status=RunStatus.OPEN)
    db.add(run)
    await db.commit()
    # hold the ids as plain values: the rollback below expires every ORM instance,
    # and touching an expired attribute afterwards triggers a lazy reload that
    # cannot run inside the async session's greenlet.
    run_id, emp_id = run.id, w["cutter"].id

    db.add(WageLine(wage_run_id=run_id, employee_id=emp_id,
                    wage_type=WageType.PIECE_RATE, amount=100))
    await db.commit()

    db.add(WageLine(wage_run_id=run_id, employee_id=emp_id,
                    wage_type=WageType.PIECE_RATE, amount=250))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()

    n = await db.scalar(select(func.count(WageLine.id))
                        .where(WageLine.wage_run_id == run_id))
    assert n == 1, "a second line for the same employee must never persist"


@pytest.mark.asyncio
async def test_the_same_employee_may_be_paid_in_two_different_runs(db):
    """The constraint is scoped to (run, employee) — not to the employee."""
    w = await _world(db)
    for i in range(2):
        s = START - timedelta(days=40 * (i + 1))
        run = WageRun(period_start=s, period_end=s + timedelta(days=13),
                      status=RunStatus.CLOSED)
        db.add(run)
        await db.flush()
        db.add(WageLine(wage_run_id=run.id, employee_id=w["cutter"].id,
                        wage_type=WageType.PIECE_RATE, amount=100))
    await db.commit()

    n = await db.scalar(select(func.count(WageLine.id))
                        .where(WageLine.employee_id == w["cutter"].id))
    assert n == 2


# ══════════════════════════════════════════════════ PIECE_RATE vs MONTHLY fork
@F139
@pytest.mark.asyncio
async def test_a_piece_worker_is_never_paid_a_salary(db):
    w = await _world(db)
    await _rate(db, w, value=12.5, effective_from=START)
    await _log(db, w, qty=40, on=WORK_DAY)

    payload = await _piece_run(db, period_start=START, period_end=END)
    line = _line_for(payload, "PIECEWORKER")

    assert line["wage_type"] == WageType.PIECE_RATE.value
    assert line["amount"] == pytest.approx(40 * 12.5)
    # A piece run carries NO salaried lines at all — the fork is the population.
    assert payload["monthly_only"] is False
    assert payload["piece_rate_only"] is True
    assert all(l["wage_type"] == WageType.PIECE_RATE.value
               for l in payload["lines"])


@F139
@pytest.mark.asyncio
async def test_a_monthly_worker_is_never_paid_per_piece(db):
    """Even with production logged against them, a MONTHLY worker is prorated.

    Now doubly guaranteed: the monthly RUN never queries production at all, and
    the monthly BRANCH still skips anyone whose wage_type is not MONTHLY."""
    w = await _world(db)
    await _rate(db, w, value=12.5, effective_from=START)
    await _log(db, w, qty=40, on=WORK_DAY, employee=w["monthly"])

    payload = await _monthly_run(db, period_start=START, period_end=END)
    line = _line_for(payload, "SALARIED")

    assert line["wage_type"] == WageType.MONTHLY.value
    assert line["amount"] != pytest.approx(40 * 12.5)
    assert payload["monthly_only"] is True
    assert payload["total_pieces"] == 0


@F139
@pytest.mark.asyncio
async def test_every_employee_gets_at_most_one_line(db):
    """One line per person — and, since the fork, across the PAIR of runs.

    A fortnight is now paid by a PIECE run plus a MONTHLY run. That is only safe
    if the two populations are genuinely disjoint: if anyone appeared on both,
    the pair would pay them twice and no single-run assertion would catch it."""
    w = await _world(db)
    await _rate(db, w, value=10, effective_from=START)
    await _log(db, w, qty=5, on=WORK_DAY)
    await _log(db, w, qty=7, on=WORK_DAY + timedelta(days=1))

    piece = await _piece_run(db, period_start=START, period_end=END)
    monthly = await _monthly_run(db, period_start=START, period_end=END)

    for payload in (piece, monthly):
        ids = [str(l["employee_id"]) for l in payload["lines"]]
        assert len(ids) == len(set(ids))

    both = ({str(l["employee_id"]) for l in piece["lines"]}
            & {str(l["employee_id"]) for l in monthly["lines"]})
    assert not both, f"paid on BOTH runs for the same window: {both}"


# ═══════════════════════════════════════════════════ date-effective rate rule
@F139
@pytest.mark.asyncio
async def test_a_midperiod_rate_change_prices_each_day_at_its_own_rate(db):
    """The best-implemented rule in the module: the cache key includes work_date
    (service.py:436-443) so a raise on day 5 does not retroactively reprice day 4.
    """
    w = await _world(db)
    day_a = START + timedelta(days=1)
    day_b = START + timedelta(days=6)
    await _rate(db, w, value=10.0, effective_from=START)
    await _rate(db, w, value=15.0, effective_from=day_b)
    await _log(db, w, qty=10, on=day_a)      # 10 x 10.00 = 100
    await _log(db, w, qty=10, on=day_b)      # 10 x 15.00 = 150

    payload = await _piece_run(db, period_start=START, period_end=END)
    line = _line_for(payload, "PIECEWORKER")
    assert line["amount"] == pytest.approx(250.0)


# ═══════════════════════════════════════════════ a closed run is frozen
@pytest.mark.asyncio
async def test_recompute_refuses_a_closed_run(db):
    """F20's guard (service.py:348). It fires — but note the audit finding: the
    `confirm_closed` escape hatch it offers is wired to NO route or schema
    (pass-02), so via the API this 409 is unconditional and recompute is dead."""
    await _world(db)
    run = WageRun(period_start=START, period_end=END, status=RunStatus.CLOSED)
    db.add(run)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await WageService(db).recompute_run(run.id, user_name="MD")
    assert exc.value.status_code == 409
    assert "closed" in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_a_closed_run_keeps_its_amounts_after_a_refused_recompute(db):
    w = await _world(db)
    run = WageRun(period_start=START, period_end=END, status=RunStatus.CLOSED)
    db.add(run)
    await db.flush()
    db.add(WageLine(wage_run_id=run.id, employee_id=w["cutter"].id,
                    wage_type=WageType.PIECE_RATE, amount=4321.00))
    await db.commit()

    with pytest.raises(HTTPException):
        await WageService(db).recompute_run(run.id, user_name="MD")

    amount = await db.scalar(select(WageLine.amount)
                             .where(WageLine.wage_run_id == run.id))
    assert float(amount) == 4321.00


# ═════════════════════════════════════════════════════════ window validation
@pytest.mark.asyncio
async def test_an_inverted_window_is_refused(db):
    await _world(db)
    # Scoped on purpose: an unscoped piece run is now ALSO a 422, and this test
    # must fail on the window rather than pass for the wrong reason.
    with pytest.raises(HTTPException) as exc:
        await _piece_run(db, period_start=END, period_end=START)
    assert exc.value.status_code == 422
    assert "period_end" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_a_future_window_is_refused(db):
    """Payroll cannot be run for work that has not happened."""
    await _world(db)
    tomorrow = date.today() + timedelta(days=1)
    with pytest.raises(HTTPException) as exc:
        await _piece_run(db, period_start=date.today(), period_end=tomorrow)
    assert exc.value.status_code == 422
    assert "future" in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_an_overlapping_closed_run_is_refused(db):
    """The same pieces must never be paid twice."""
    await _world(db)
    db.add(WageRun(period_start=START, period_end=END, status=RunStatus.CLOSED))
    await db.commit()

    # The pre-existing run carries run_kind='combined' (the model default), which
    # is what a run computed before the piece/monthly split really was: it paid
    # both populations for the whole factory. It therefore collides with anything.
    with pytest.raises(HTTPException) as exc:
        await _piece_run(db, period_start=START + timedelta(days=3),
                         period_end=END)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_an_abandoned_open_run_still_blocks_the_window(db):
    """B7 / prior F22: a run that died mid-population sits at status=OPEN with
    money already committed. If the overlap check only looked at CLOSED runs, the
    retry would pay the fortnight a second time."""
    await _world(db)
    db.add(WageRun(period_start=START, period_end=END, status=RunStatus.OPEN))
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await _piece_run(db, period_start=START, period_end=END)
    assert exc.value.status_code == 409
    assert "open" in str(exc.value.detail).lower()


@F139
@pytest.mark.asyncio
async def test_a_non_overlapping_earlier_window_is_allowed(db):
    """Adjacent fortnights are the normal case and must not be blocked."""
    await _world(db)
    db.add(WageRun(period_start=START - timedelta(days=30),
                   period_end=START - timedelta(days=17), status=RunStatus.CLOSED))
    await db.commit()

    payload = await _piece_run(db, period_start=START, period_end=END)
    assert payload["period_start"] == START or str(START) in str(payload)
