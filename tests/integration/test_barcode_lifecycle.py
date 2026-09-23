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
        sku_id=pieces[0].sku_id, operation_id=operations["LEATHER_CUTTING"].id,
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
async def test_a_non_numeric_code_does_not_jam_minting_for_that_prefix(db):
    """A non-numeric code in the namespace must not stop the counter.

    THIS TEST USED TO ASSERT THE JAM, and said so: "This test pins the current
    behaviour, so the fix is a visible change." This is that change.

    `_next_code` derives the next counter from the lexicographically highest
    code matching `PREFIX-%`. It took LIMIT 1 and fell back to 0 when that one
    code's tail was not all digits, which is a PERMANENT jam rather than a
    one-off bad number: the first mint after it returns `EMP-000001`, and so
    does the second, which dies on the unique index. That is an unhandled
    IntegrityError — HTTP 500 — on every employee create and every card reissue
    from then on, with no way out through the API.

    It now walks down past non-numeric tails. That is correct rather than a
    heuristic: numeric tails are zero-padded to a fixed width, so among them
    lexicographic order is numeric order, and skipping other rows cannot
    reorder them.

    THE PRECONDITION IS CONSTRUCTED HERE AND THE TEST TAKES NO EMPLOYEE FIXTURE.
    Both matter, and the second was learned the hard way.

    It first asserted that `cutter`'s auto-generated `EMP-<uuid hex>` had a
    non-numeric tail — true only ~94% of the time, since six hex characters are
    all digits about 6% of the time. Dropping that assertion was not enough:
    merely REQUESTING the fixture still puts a random `EMP-<hex>` in the table,
    and when those six characters happen to be digits they form a number far
    larger than the 42 seeded below, so the next code is EMP-573197 rather than
    EMP-000043. Same 6% flake, different cause.

    So this test owns every EMP- row that exists while it runs. A test that
    asserts an exact generated code cannot share a namespace with a fixture that
    generates random ones.
    """
    from app.core.enums import BarcodeStatus, BarcodeType
    from app.modules.barcode.models import BarcodeRegistry
    from app.modules.employees import schemas as eschemas
    from app.modules.employees.service import EmployeeService

    # A numeric high-water mark, and a letter-tailed code that sorts ABOVE it.
    db.add(BarcodeRegistry(code="EMP-000042", type=BarcodeType.EMPLOYEE.value,
                           status=BarcodeStatus.ACTIVE.value, caption="numeric"))
    db.add(BarcodeRegistry(code="EMP-ZZZZZZ", type=BarcodeType.EMPLOYEE.value,
                           status=BarcodeStatus.ACTIVE.value, caption="letters"))
    await db.commit()

    first = await EmployeeService(db).create(eschemas.EmployeeCreate(
        name="AFTER JAM ONE", designation="CUTTER",
        wage_type=WageType.PIECE_RATE))
    # continues from the real numeric maximum, not from zero
    assert first.employee_barcode == "EMP-000043"

    second = await EmployeeService(db).create(eschemas.EmployeeCreate(
        name="AFTER JAM TWO", designation="CUTTER",
        wage_type=WageType.PIECE_RATE))
    assert second.employee_barcode == "EMP-000044"


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
    piece = pieces[0]
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
    piece = pieces[0]
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
async def test_a_piece_code_is_rejected_by_the_lot_door(db, pieces):
    """A narrow resolver must refuse a code of the wrong type, not answer with
    whatever FK happens to be populated. This used to use a DRAWER code; the
    piece code is the one every operator has and is just as wrong for this door.
    """
    piece = pieces[0]
    with pytest.raises(HTTPException) as exc:
        await BarcodeService(db).resolve_lot_id(piece.code)
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
