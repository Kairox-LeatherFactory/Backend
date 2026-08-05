"""
INTEGRATION · the roster read carries each worker's card code.

WHY THE LIST NEEDS IT
    The employee list IS the barcode screen: clicking a row goes on to
    PATCH /employees/{employee_id}/barcode (reissue / deactivate). Without the
    code on the row the user is retiring a card they cannot see, and the printed
    card in their hand cannot be matched to the row they are about to act on.

WHAT `employee_barcode` MEANS
    The ACTIVE card, or None. A retired code is not scannable — resolve() gives
    410 Gone (CLAUDE.md §6) — so showing it on the roster would invite a scan
    that can only fail. After a reissue the row must show the NEW code, never
    the retired one.
"""
import pytest
from sqlalchemy import select

from app.core.enums import BarcodeStatus, WageType
from app.modules.barcode.models import BarcodeRegistry
from app.modules.barcode.service import BarcodeService
from app.modules.employees import schemas
from app.modules.employees.service import EmployeeService

pytestmark = pytest.mark.integrity


async def _create(db, name, designation="CUTTER"):
    return await EmployeeService(db).create(schemas.EmployeeCreate(
        name=name, designation=designation, wage_type=WageType.PIECE_RATE))


@pytest.mark.asyncio
async def test_the_roster_returns_the_code_that_was_minted_at_create(db):
    created = await _create(db, "ROSTER ONE")

    svc = EmployeeService(db)
    rows = await svc.list_all(active_only=True)
    codes = await svc.barcodes_for([r.id for r in rows])

    assert codes[created.id] == created.employee_barcode


@pytest.mark.asyncio
async def test_one_query_covers_the_whole_roster(db):
    """The batch shape is the point — a per-row lookup would be N queries behind
    a list endpoint that HR opens all day."""
    a = await _create(db, "ROSTER MANY A")
    b = await _create(db, "ROSTER MANY B")
    c = await _create(db, "ROSTER MANY C")

    codes = await EmployeeService(db).barcodes_for([a.id, b.id, c.id])

    assert {codes[a.id], codes[b.id], codes[c.id]} == {
        a.employee_barcode, b.employee_barcode, c.employee_barcode}
    assert len(set(codes.values())) == 3, "two workers cannot share one card"


@pytest.mark.asyncio
async def test_after_a_reissue_the_roster_shows_the_new_card_not_the_retired_one(db):
    created = await _create(db, "ROSTER REISSUED")
    old_code = created.employee_barcode

    out = await BarcodeService(db).reissue_employee_barcode(
        created.id, actor_id=None)

    codes = await EmployeeService(db).barcodes_for([created.id])
    assert codes[created.id] == out["employee_barcode"]
    assert codes[created.id] != old_code, (
        "the roster is offering a card that resolve() answers 410 for")


@pytest.mark.asyncio
async def test_a_deactivated_card_leaves_the_row_with_no_code(db):
    """The leaver keeps their row and their wage history (CLAUDE.md §6); what
    they lose is the scannable code, so the field goes None rather than showing
    a dead one."""
    created = await _create(db, "ROSTER LEAVER")
    await BarcodeService(db).deactivate_employee_barcode(
        created.id, actor_id=None)

    codes = await EmployeeService(db).barcodes_for([created.id])
    assert created.id not in codes

    # the row itself is untouched — still listable, just without a live card
    rows = await EmployeeService(db).list_all(active_only=True)
    assert created.id in {r.id for r in rows}

    # and the retired row survives for the audit trail
    retired = await db.scalar(select(BarcodeRegistry.status).where(
        BarcodeRegistry.employee_id == created.id))
    assert retired == BarcodeStatus.RETIRED.value


@pytest.mark.asyncio
async def test_an_employee_with_no_card_is_absent_from_the_map_not_an_error(db):
    """Pre-barcode rows exist. A missing card is None on the row, never a 500."""
    from app.modules.employees.models import Employee
    emp = Employee(name="ROSTER NOCARD", designation="CUTTER",
                   wage_type=WageType.PIECE_RATE, is_active=True)
    db.add(emp)
    await db.commit()
    await db.refresh(emp)

    codes = await EmployeeService(db).barcodes_for([emp.id])
    assert codes.get(emp.id) is None


@pytest.mark.asyncio
async def test_the_empty_roster_asks_the_database_nothing(db):
    assert await EmployeeService(db).barcodes_for([]) == {}
