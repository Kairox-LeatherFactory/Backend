"""
INTEGRATION · the employee-barcode lifecycle — "history is sacred" (CLAUDE.md §6).

THE RULE THIS FILE DEFENDS
    You delete the scannable CODE, never the person or their record. A worker who
    leaves still cut 400 pieces last fortnight and is still owed for them. So:

      reissue     retire the old code, mint a new one, history untouched
      deactivate  flip the registry status to RETIRED -> resolve() returns 410
                  while the employee row, every production event and every wage
                  line stay exactly where they were

    410 vs 404 is load-bearing: "this card was deactivated" and "this code never
    existed" are different facts, and the UI says different things about them
    (barcode/service.py:38-52).
"""
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.core.enums import BarcodeStatus, BarcodeType, RunStatus, WageType
from app.modules.barcode.models import BarcodeRegistry
from app.modules.barcode.service import BarcodeService
from app.modules.production.models import ProductionEvent
from app.modules.wages.models import WageLine, WageRun

pytestmark = pytest.mark.integrity


async def _code_row(db, employee_id, *, active=True):
    stmt = select(BarcodeRegistry).where(
        BarcodeRegistry.employee_id == employee_id)
    if active:
        stmt = stmt.where(BarcodeRegistry.status == BarcodeStatus.ACTIVE.value)
    return (await db.execute(stmt)).scalars().first()


# ══════════════════════════════════════════════════════════════ happy path
@pytest.mark.asyncio
async def test_resolving_an_active_employee_card_returns_the_worker(db, cutter):
    emp, bc = cutter
    out = await BarcodeService(db).resolve(bc.code)
    assert out["type"] == BarcodeType.EMPLOYEE.value
    assert out["active"] is True
    assert out["employee"]["name"] == "RAMESH"
    assert out["employee"]["is_active"] is True


@pytest.mark.asyncio
async def test_an_unknown_code_is_404_not_a_guess(db):
    """barcode/service.py:56-57. A scanner that produced junk must be told so —
    returning a nearest match would attribute someone's work to a stranger."""
    with pytest.raises(HTTPException) as exc:
        await BarcodeService(db).resolve("NOT-A-REAL-CODE")
    assert exc.value.status_code == 404


# ══════════════════════════════════ reissue: lost card, same person, same history
@pytest.mark.asyncio
async def test_reissue_mints_a_new_code_and_retires_the_old_one(db, cutter):
    emp, old = cutter
    old_code = old.code

    out = await BarcodeService(db).reissue_employee_barcode(emp.id, actor_id=None)

    assert out["employee_barcode"] != old_code
    assert out["history_preserved"] is True

    # the old card is gone from service, but the row survives for the audit trail
    with pytest.raises(HTTPException) as exc:
        await BarcodeService(db).resolve(old_code)
    assert exc.value.status_code == 410

    # the new card resolves to the SAME employee
    fresh = await BarcodeService(db).resolve(out["employee_barcode"])
    assert fresh["employee"]["employee_id"] == str(emp.id)


@pytest.mark.asyncio
async def test_reissue_does_not_touch_the_work_already_logged(db, cutter,
                                                              operations, pieces):
    """The point of the whole design: a new plastic card is not a new person."""
    emp, _ = cutter
    db.add(ProductionEvent(
        sku_id=pieces[0][0].sku_id, operation_id=operations["LEATHER_CUTTING"].id,
        employee_id=emp.id, work_date=__import__("datetime").date.today(),
        qty=40, entered_by="test"))
    await db.commit()

    await BarcodeService(db).reissue_employee_barcode(emp.id, actor_id=None)

    n = await db.scalar(select(func.count(ProductionEvent.id))
                        .where(ProductionEvent.employee_id == emp.id))
    assert n == 1, "reissuing a card destroyed the worker's production history"


@pytest.mark.asyncio
async def test_reissuing_twice_leaves_exactly_one_active_card(db):
    """Two live cards for one worker means two people can clock in as them.

    The employee is created through `EmployeeService` rather than the `cutter`
    fixture on purpose: the fixture hand-writes `EMP-<hex-slug>`
    (conftest.py:170-172), which is NOT the shape production mints, and that
    shape jams the counter (see the F150 test below). Using the real create path
    keeps THIS test about the reissue invariant instead of about code formats.
    """
    from app.modules.employees import schemas as eschemas
    from app.modules.employees.service import EmployeeService

    created = await EmployeeService(db).create(eschemas.EmployeeCreate(
        name="REISSUED TWICE", designation="CUTTER",
        wage_type=WageType.PIECE_RATE))

    svc = BarcodeService(db)
    first = await svc.reissue_employee_barcode(created.id, actor_id=None)
    second = await svc.reissue_employee_barcode(created.id, actor_id=None)
    assert first["employee_barcode"] != second["employee_barcode"]

    active = await db.scalar(
        select(func.count(BarcodeRegistry.id)).where(
            BarcodeRegistry.employee_id == created.id,
            BarcodeRegistry.status == BarcodeStatus.ACTIVE.value))
    assert active == 1

    total = await db.scalar(
        select(func.count(BarcodeRegistry.id)).where(
            BarcodeRegistry.employee_id == created.id))
    assert total == 3, "the retired cards must survive for the audit trail"


@pytest.mark.asyncio
async def test_one_non_numeric_code_jams_minting_for_that_prefix_forever(db, cutter):
    """REPRODUCES PRIOR FINDING F16/F79/F99 (still OPEN — pass-07-repository-
    layer.md:55-68, delta-register.md:61), and sharpens its stated consequence.

    pass-07 predicts that "the ordering is lexical on a string code, so a format
    change ... silently returns the wrong maximum". This is that prediction,
    executed — and the consequence is worse than "wrong maximum": it is a
    PERMANENT jam, not a one-off bad number.

    `_next_code` (barcode/repository.py:53-76) derives the next counter from the
    LEXICOGRAPHICALLY highest code matching `PREFIX-%`, then falls back to 0 when
    that code's tail is not all digits:

        top = ... ORDER BY code DESC LIMIT 1
        if tail.isdigit(): mx = int(tail)      # else mx stays 0
        return f"{prefix}-{mx + 1:06d}"

    So a SINGLE code in the namespace whose tail sorts high and is not numeric
    pins the counter at 0 permanently. The first mint after that returns
    `EMP-000001`; the second returns `EMP-000001` again and dies on the unique
    index — an unhandled IntegrityError (HTTP 500) on EVERY subsequent employee
    create and card reissue, forever, with no way out through the API.

    The docstring's premise ("the zero-padded numeric tail is fixed-width, so
    lexicographic order == numeric order") holds only while EVERY code in the
    prefix is numeric-tailed. Nothing enforces that: `register_nocommit`
    (repository.py:79-91) accepts any string, and the column has no CHECK.

    In app code `_next_code` is currently the only EMP writer, so this needs a
    migrated, seeded or hand-inserted row to fire — but the shared test fixture
    writes exactly such a row (`EMP-{uuid[:6].upper()}`, conftest.py:170-172),
    which is how readily the shape occurs. A max over the numeric tail, or a real
    sequence column, removes the class.

    This test pins the current behaviour, so the fix is a visible change.
    """
    from sqlalchemy.exc import IntegrityError
    from app.modules.employees import schemas as eschemas
    from app.modules.employees.service import EmployeeService

    # `cutter` has already put EMP-<hex> in the table (tail is not all digits).
    emp, bc = cutter
    assert not bc.code[4:].isdigit(), "fixture no longer reproduces the precondition"

    first = await EmployeeService(db).create(eschemas.EmployeeCreate(
        name="AFTER JAM ONE", designation="CUTTER",
        wage_type=WageType.PIECE_RATE))
    assert first.employee_barcode == "EMP-000001"

    with pytest.raises(IntegrityError):
        await EmployeeService(db).create(eschemas.EmployeeCreate(
            name="AFTER JAM TWO", designation="CUTTER",
            wage_type=WageType.PIECE_RATE))
    await db.rollback()


# ═══════════════════════════════ deactivate: the leaver, history intact
@pytest.mark.asyncio
async def test_deactivating_a_card_leaves_the_employee_and_their_wages_intact(
        db, cutter):
    """CLAUDE.md §6: 'The employee row, all production events, and all wage lines
    stay intact.' A closed wage line is a document that was already paid
    against — deleting it because someone resigned would destroy the record of a
    payment the factory actually made."""
    emp, bc = cutter
    run = WageRun(period_start=__import__("datetime").date.today(),
                  period_end=__import__("datetime").date.today(),
                  status=RunStatus.CLOSED)
    db.add(run)
    await db.flush()
    db.add(WageLine(wage_run_id=run.id, employee_id=emp.id,
                    wage_type=WageType.PIECE_RATE, pieces=40, amount=500.00))
    await db.commit()

    out = await BarcodeService(db).deactivate_employee_barcode(emp.id, actor_id=None)
    assert out["active"] is False
    assert out["history_preserved"] is True

    from app.modules.employees.models import Employee
    assert await db.get(Employee, emp.id) is not None, "the person was deleted"
    amount = await db.scalar(select(WageLine.amount)
                             .where(WageLine.employee_id == emp.id))
    assert float(amount) == 500.00, "a closed wage line was destroyed with the card"


@pytest.mark.asyncio
async def test_a_retired_card_is_410_gone_not_404(db, cutter):
    """The distinction the UI depends on (barcode/service.py:59-65): 410 lets it
    say 'this card was deactivated'; 404 would say 'invalid barcode' and send a
    supervisor hunting for a scanner fault that does not exist."""
    emp, bc = cutter
    await BarcodeService(db).deactivate_employee_barcode(emp.id, actor_id=None)

    with pytest.raises(HTTPException) as exc:
        await BarcodeService(db).resolve(bc.code)
    assert exc.value.status_code == 410
    assert "deactivated" in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_deactivating_twice_is_a_404_not_a_second_retirement(db, cutter):
    """There is no active card left to retire (barcode/service.py:237-240)."""
    emp, _ = cutter
    svc = BarcodeService(db)
    await svc.deactivate_employee_barcode(emp.id, actor_id=None)

    with pytest.raises(HTTPException) as exc:
        await BarcodeService(db).deactivate_employee_barcode(emp.id, actor_id=None)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_a_retired_card_cannot_be_used_to_log_production(db, cutter):
    """`resolve_employee_id` is the /production/log actor door
    (production/router.py:132). F18 routed it through the shared 410 lookup so a
    leaver's card cannot keep booking work after they are gone."""
    emp, bc = cutter
    await BarcodeService(db).deactivate_employee_barcode(emp.id, actor_id=None)

    with pytest.raises(HTTPException) as exc:
        await BarcodeService(db).resolve_employee_id(bc.code)
    assert exc.value.status_code == 410


# ══════════════════════════════════ F18 · retirement applies to EVERY type
@pytest.mark.asyncio
async def test_a_retired_piece_label_also_reports_410(db, pieces):
    """F18 (barcode/service.py:59-65): retirement is a lifecycle state on the
    registry, not an employee-only concept. A reprinted piece label whose old
    code is still stuck to a garment must not silently resolve."""
    piece, _ = pieces[0]
    row = (await db.execute(select(BarcodeRegistry)
                            .where(BarcodeRegistry.piece_id == piece.id))
           ).scalars().first()
    row.status = BarcodeStatus.RETIRED.value
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await BarcodeService(db).resolve(piece.code)
    assert exc.value.status_code == 410

    with pytest.raises(HTTPException) as exc:
        await BarcodeService(db).resolve_piece_id(piece.code)
    assert exc.value.status_code == 410


@pytest.mark.asyncio
async def test_a_piece_code_is_rejected_by_the_employee_door(db, pieces):
    """Type confusion at the scan door: scanning a garment where the actor is
    expected must 404, not resolve to whatever id happens to be populated
    (barcode/service.py:95-98)."""
    piece, _ = pieces[0]
    with pytest.raises(HTTPException) as exc:
        await BarcodeService(db).resolve_employee_id(piece.code)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_an_employee_code_is_rejected_by_the_piece_door(db, cutter):
    emp, bc = cutter
    with pytest.raises(HTTPException) as exc:
        await BarcodeService(db).resolve_piece_id(bc.code)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_a_drawer_code_is_rejected_by_the_lot_door(db, pieces):
    _, drawer = pieces[0]
    with pytest.raises(HTTPException) as exc:
        await BarcodeService(db).resolve_lot_id(drawer.code)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_reissue_for_an_employee_who_never_had_a_card_still_mints_one(db):
    """`reissue` tolerates `old is None` (barcode/service.py:222-226). An employee
    created before the barcode feature must be issuable a first card through the
    same door rather than needing a special case."""
    from app.modules.employees.models import Employee
    emp = Employee(name="NOCARD", designation="CUTTER",
                   wage_type=WageType.PIECE_RATE, is_active=True)
    db.add(emp)
    await db.commit()
    await db.refresh(emp)

    out = await BarcodeService(db).reissue_employee_barcode(emp.id, actor_id=None)
    assert out["employee_barcode"]
    resolved = await BarcodeService(db).resolve(out["employee_barcode"])
    assert resolved["employee"]["employee_id"] == str(emp.id)
