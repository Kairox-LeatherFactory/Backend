"""
INTEGRATION · payroll's SILENT-EXCLUSION surface and the recompute safety net.

`test_wage_run_money_path.py` already owns the fork, the window guards and the
uniqueness constraint. This file covers what happens to the people those rules
QUIETLY DROP, and what happens when a recompute fails halfway.

WHY THIS IS THE HIGHEST-VALUE GAP
    Every exclusion here produces a worker who is paid ₹0.00 and no error. A
    payroll bug that raises is found on the day it ships; a payroll bug that
    silently omits one line is found by the worker, weeks later, and the factory
    has already paid out. The service knows this — H12/H13 added
    `unrated_operations` and `excluded_untyped_employees` to the run payload
    precisely so nothing leaves without a name attached (service.py:516-522).
    These tests assert those channels actually carry the names.

MARKERS: money (all of it decides what a person is paid).
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

pytestmark = pytest.mark.money

# Same window convention as the sibling money-path file: must end on or before
# today (_validate_window, wages/service.py:290-294).
END = date.today() - timedelta(days=1)
START = END - timedelta(days=13)
WORK_DAY = START + timedelta(days=2)


async def _world(db):
    """One style, one operation, and a cast of employees covering every branch
    of `_populate_run` — including the ones that fall off the end of it."""
    client = cm.Client(name="ExclusionCo")
    db.add(client)
    await db.flush()
    po = cm.ClientOrder(client_id=client.id, order_number="EXC-PO")
    db.add(po)
    await db.flush()
    style = cm.Style(client_order_id=po.id, name="ASHFORD", code="ASHFORD")
    db.add(style)
    await db.flush()
    sku = cm.SKU(style_id=style.id, color_code="12", size="L", qty_ordered=50,
                 code="EXC-PO-ASHFORD-12-L")
    db.add(sku)
    op = pm.Operation(code="LEATHER_CUTTING", label="Leather Cutting", sequence=1)
    db.add(op)
    await db.flush()

    people = {
        "piece": em.Employee(name="PIECEWORKER", designation="CUTTER",
                             wage_type=WageType.PIECE_RATE, is_active=True),
        "leaver_piece": em.Employee(name="LEAVER_PIECE", designation="CUTTER",
                                    wage_type=WageType.PIECE_RATE, is_active=False),
        "monthly": em.Employee(name="SALARIED", designation="TAILOR",
                               wage_type=WageType.MONTHLY, monthly_salary=30000,
                               is_active=True),
        "leaver_monthly": em.Employee(name="LEAVER_SALARIED", designation="TAILOR",
                                      wage_type=WageType.MONTHLY,
                                      monthly_salary=30000, is_active=False),
        "no_salary": em.Employee(name="NOSALARY", designation="TAILOR",
                                 wage_type=WageType.MONTHLY,
                                 monthly_salary=None, is_active=True),
    }
    db.add_all(list(people.values()))
    await db.commit()
    for o in (style, sku, op, *people.values()):
        await db.refresh(o)
    return dict(style=style, sku=sku, op=op, **people)


async def _log(db, w, *, qty, on, employee):
    db.add(pm.ProductionEvent(
        sku_id=w["sku"].id, operation_id=w["op"].id, employee_id=employee.id,
        work_date=on, qty=qty, entered_by="test"))
    await db.commit()


async def _rate(db, w, *, value, effective_from):
    db.add(Rate(style_id=w["style"].id, operation_id=w["op"].id,
                rate=value, effective_from=effective_from))
    await db.commit()


def _line(payload, name):
    return next((l for l in payload["lines"] if l["employee_name"] == name), None)


# ═══════════════════════════════ H13 · the leaver's final fortnight
@pytest.mark.asyncio
async def test_a_deactivated_piece_worker_is_still_paid_for_what_they_cut(db):
    """H13 (service.py:406-410): `active_only=True` used to drop anyone
    deactivated between working and payday. Their pieces then failed the
    wage_type lookup and vanished with no warning.

    A leaver's final fortnight is the single most disputed payslip in a factory —
    the pieces are on the floor, the evidence is in production_event, and the
    money is owed regardless of whether HR has already flipped is_active."""
    w = await _world(db)
    await _rate(db, w, value=12.5, effective_from=START)
    await _log(db, w, qty=40, on=WORK_DAY, employee=w["leaver_piece"])

    payload = await WageService(db).compute_run(START, END)
    line = _line(payload, "LEAVER_PIECE")

    assert line is not None, "a leaver's piece money was dropped from payroll"
    assert line["amount"] == pytest.approx(40 * 12.5)


@pytest.mark.asyncio
async def test_a_deactivated_monthly_worker_is_not_paid_a_salary(db):
    """The other half of H13 (service.py:471-475), and it must NOT be symmetric.

    Piece money is evidenced by production events the worker actually produced.
    A monthly line has no such evidence — proration is calendar-only, so paying a
    leaver a prorated salary pays someone who did not work. The asymmetry is the
    correct behaviour, so it is asserted rather than assumed."""
    w = await _world(db)
    payload = await WageService(db).compute_run(START, END)

    assert _line(payload, "LEAVER_SALARIED") is None
    assert _line(payload, "SALARIED") is not None, "an active monthly worker was dropped"


# ══════════════════════════════ H12 · a NULL salary is a data error, not ₹0.00
@pytest.mark.asyncio
async def test_a_monthly_worker_with_no_salary_is_reported_not_zeroed(db):
    """H12 (service.py:478-481). Emitting a ₹0.00 line would count toward
    `employee_count` and print a payslip that looks deliberate. Instead the
    employee is excluded and NAMED under `unrated_operations` with
    kind='monthly_salary_missing', so someone fixes the record."""
    w = await _world(db)
    payload = await WageService(db).compute_run(START, END)

    assert _line(payload, "NOSALARY") is None, "a NULL salary produced a payslip line"

    flagged = [u for u in payload["unrated_operations"]
               if u.get("kind") == "monthly_salary_missing"]
    assert len(flagged) == 1
    assert flagged[0]["employee_name"] == "NOSALARY", (
        "the warning must name the employee — a raw UUID is unactionable")


# ══════════════════════════════ H13 · an unrecognised wage_type is surfaced
@pytest.mark.asyncio
async def test_an_out_of_vocabulary_wage_type_crashes_payroll_instead_of_excluding(db):
    """FINDING F148. The H13 safety net does not reach the failure it was built
    for, and the real failure is worse than the one it handles.

    The design (wages/service.py:65-81, :416-419, :518-522) is: an unreadable
    wage_type coerces to None, fails both branch tests, and the worker is NAMED
    under `excluded_untyped_employees` so nobody is silently paid nothing. The
    docstring at service.py:66-73 justifies this by saying raw strings 'have
    leaked through before'.

    But `Employee.wage_type` is `Mapped[WageType]` over `Enum(WageType)`
    (employees/models.py:34-36) — non-nullable, with SQLAlchemy's own value
    lookup on READ. So a legacy row holding 'daily' raises LookupError while
    hydrating the Employee, inside `employees.list_all` (service.py:410), BEFORE
    `_as_wage_type` is ever called. That is an unhandled 500 that takes down the
    ENTIRE run — every other employee's pay included — rather than excluding one
    person and reporting them.

    So `excluded_untyped_employees` is unreachable through the ORM, and the
    branch it guards is dead code. This test pins the real behaviour; if the
    read is later made defensive, it fails and that is the signal to re-point it
    at the exclusion list.
    """
    w = await _world(db)
    # A legacy row or a hand-run UPDATE can hold a value the enum never had.
    # Written at the SQL level because the ORM refuses it on the way in too.
    await db.execute(
        em.Employee.__table__.update()
        .where(em.Employee.id == w["piece"].id)
        .values(wage_type="daily")
    )
    await db.commit()
    db.expire_all()

    with pytest.raises(LookupError, match="not among the defined enum values"):
        await WageService(db).compute_run(START, END)


@pytest.mark.asyncio
async def test_the_untyped_exclusion_channel_reports_nobody_on_a_clean_roster(db):
    """The positive control for the test above: with every wage_type valid, the
    exclusion list is empty. Together they show the channel is well-formed but
    unreachable — it is not that the roster happens to be clean."""
    await _world(db)
    payload = await WageService(db).compute_run(START, END)
    assert payload["excluded_untyped_employees"] == []


# ══════════════════════════════ unrated operations are named, not counted
@pytest.mark.asyncio
async def test_unpriced_work_is_reported_against_a_style_code_not_a_uuid(db):
    """`_name_unrated` (service.py:527-537) exists so the manager can reach the
    rate sheet that fixes the problem. A UUID in this warning is unactionable."""
    w = await _world(db)
    await _log(db, w, qty=17, on=WORK_DAY, employee=w["piece"])   # no rate set

    payload = await WageService(db).compute_run(START, END)
    unrated = [u for u in payload["unrated_operations"]
               if u.get("kind") == "unrated_operation"]

    assert len(unrated) == 1
    assert unrated[0]["style_code"] == "ASHFORD"
    assert unrated[0]["operation_code"] == "LEATHER_CUTTING"
    assert unrated[0]["unpaid_pieces"] == 17
    assert _line(payload, "PIECEWORKER") is None, (
        "unpriced work must not become a zero-rupee line")


# ══════════════════════════════ H11 · the printed line must reconcile
@pytest.mark.asyncio
async def test_a_blended_rate_line_reconciles_pieces_times_rate_to_amount(db):
    """H11 (service.py:450-457). When a rate changes mid-period the pieces were
    priced at several rates. Storing 'last rate wins' made the printed payslip
    read `pieces × rate != amount`, and WHICH rate showed depended on the query
    plan. The stored rate is now the blended `amount / pieces`, which always
    reconciles — that is what makes a payslip defensible to the worker holding
    it."""
    w = await _world(db)
    day_a = START + timedelta(days=1)
    day_b = START + timedelta(days=6)
    await _rate(db, w, value=10.0, effective_from=START)
    await _rate(db, w, value=15.0, effective_from=day_b)
    await _log(db, w, qty=10, on=day_a, employee=w["piece"])
    await _log(db, w, qty=10, on=day_b, employee=w["piece"])

    payload = await WageService(db).compute_run(START, END)
    line = _line(payload, "PIECEWORKER")

    assert line["pieces"] == 20
    assert line["amount"] == pytest.approx(250.0)
    row = line["breakdown"][0]
    assert row["pieces"] * row["rate"] == pytest.approx(row["amount"], abs=0.01), (
        "the payslip breakdown does not reconcile: pieces x rate != amount")
    assert row["rate"] == pytest.approx(12.5), "expected the blended rate, not 10 or 15"


# ══════════════════════════════ B6 · recompute is guarded, audited, reversible
@pytest.mark.asyncio
async def test_recompute_of_a_closed_run_requires_explicit_confirmation(db):
    """B6 (service.py:347-353). The 409 is not a refusal to ever recompute — it
    is a demand that the caller SAY, in the request, that they are rewriting a
    document already paid against."""
    w = await _world(db)
    await _rate(db, w, value=10.0, effective_from=START)
    await _log(db, w, qty=5, on=WORK_DAY, employee=w["piece"])
    first = await WageService(db).compute_run(START, END)

    with pytest.raises(HTTPException) as exc:
        await WageService(db).recompute_run(first["id"], user_name="DM")
    assert exc.value.status_code == 409

    again = await WageService(db).recompute_run(
        first["id"], user_name="DM", confirm_closed=True)
    assert again["recomputed"] is True
    assert again["recompute_count"] == 1


@pytest.mark.asyncio
async def test_a_recompute_stamps_who_did_it_so_a_reprint_is_identifiable(db):
    """A payslip reprinted after a recompute must be visibly a DIFFERENT document
    from the one paid against (service.py:336-338), or the factory cannot tell
    which version the cash matched."""
    w = await _world(db)
    await _rate(db, w, value=10.0, effective_from=START)
    await _log(db, w, qty=5, on=WORK_DAY, employee=w["piece"])
    run = await WageService(db).compute_run(START, END)

    await WageService(db).recompute_run(run["id"], user_name="AUDITOR",
                                        confirm_closed=True)
    detail = await WageService(db).get_run_detail(run["id"])

    assert detail["recomputed"] is True
    assert detail["recompute_count"] == 1
    assert detail["last_recomputed_by"] == "AUDITOR"
    assert detail["last_recomputed_at"] is not None


@pytest.mark.asyncio
async def test_a_failed_recompute_restores_the_lines_it_deleted(db):
    """B6's snapshot-and-restore (service.py:360-376). `clear_lines()` COMMITS,
    so without the restore a crash inside `_populate_run` leaves a CLOSED run
    with zero lines for a fortnight already in envelopes.

    The failure is injected at `_populate_run` rather than by corrupting data,
    because the guarantee under test is 'ANY exception restores', not 'this
    particular exception restores'."""
    w = await _world(db)
    await _rate(db, w, value=10.0, effective_from=START)
    await _log(db, w, qty=5, on=WORK_DAY, employee=w["piece"])

    svc = WageService(db)
    run = await svc.compute_run(START, END)
    before = await db.scalar(
        select(func.count(WageLine.id)).where(WageLine.wage_run_id == run["id"]))
    assert before > 0

    async def boom(*a, **kw):
        raise RuntimeError("simulated failure mid-repopulate")

    svc._populate_run = boom
    with pytest.raises(RuntimeError):
        await svc.recompute_run(run["id"], user_name="DM", confirm_closed=True)

    after = await db.scalar(
        select(func.count(WageLine.id)).where(WageLine.wage_run_id == run["id"]))
    assert after == before, (
        "a crashed recompute left the closed run empty — the paid-against lines "
        "were destroyed and not restored")


@pytest.mark.asyncio
async def test_recompute_of_a_missing_run_is_a_404(db):
    import uuid
    await _world(db)
    with pytest.raises(HTTPException) as exc:
        await WageService(db).recompute_run(uuid.uuid4(), user_name="DM")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_a_failed_compute_leaves_no_orphan_run_blocking_the_window(db):
    """compute_run deletes its own run on failure (service.py:391-393). If it did
    not, the abandoned OPEN run would trip the B7 overlap guard and the manager
    could never retry the fortnight."""
    w = await _world(db)
    svc = WageService(db)

    async def boom(*a, **kw):
        raise RuntimeError("simulated failure during populate")

    svc._populate_run = boom
    with pytest.raises(RuntimeError):
        await svc.compute_run(START, END)

    orphans = await db.scalar(
        select(func.count(WageRun.id))
        .where(WageRun.period_start == START, WageRun.period_end == END))
    assert orphans == 0, "a failed run was left behind and now blocks its own window"

    # and the retry succeeds
    payload = await WageService(db).compute_run(START, END)
    assert payload["status"] == RunStatus.CLOSED
