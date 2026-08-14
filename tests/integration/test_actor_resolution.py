"""
INTEGRATION · resolving WHO did the work, across both doors.

THE REPORT THAT PROMPTED THIS
    A production log came back "Employee not found." for an employee the caller
    could see in the database. The request carried both doors:

        employee_barcode : EMP-000033          -> a real, active card
        employee_id      : f761b42d-...        -> a `barcode_registry` ROW id

    Two separate faults met. The id was from the wrong table — a barcode row's
    own id, which is a UUID that looks exactly like an employee's. And the router
    silently preferred the id and discarded the barcode, so a request containing
    everything needed to succeed failed with a message that named neither
    problem.

    Worse than the 404: the two fields named DIFFERENT PEOPLE. Had the id been a
    valid employee, the scan of one worker's card would have been logged against
    another, silently, and the wage run would have paid the wrong person.

WHAT IS PINNED
    1. both doors sent and disagreeing  -> 422 naming both people
    2. both sent and agreeing           -> fine
    3. an id from the wrong table       -> 404 that says which id to use
    4. either door alone                -> fine
"""
import uuid

import pytest
from fastapi import HTTPException

from app.modules.barcode.service import BarcodeService

pytestmark = pytest.mark.integrity


def _code(emp) -> str:
    return f"EMP-{str(emp.id)[:6].upper()}"


@pytest.mark.asyncio
async def test_the_barcode_alone_resolves(db, cutter):
    emp, card = cutter
    got = await BarcodeService(db).resolve_actor(
        employee_barcode=card.code, employee_id=None)
    assert got == emp.id


@pytest.mark.asyncio
async def test_the_id_alone_resolves(db, cutter):
    emp, _ = cutter
    got = await BarcodeService(db).resolve_actor(
        employee_barcode=None, employee_id=emp.id)
    assert got == emp.id


@pytest.mark.asyncio
async def test_both_doors_agreeing_is_fine(db, cutter):
    """The normal frontend case: it scanned the card AND knows the id."""
    emp, card = cutter
    got = await BarcodeService(db).resolve_actor(
        employee_barcode=card.code, employee_id=emp.id)
    assert got == emp.id


@pytest.mark.asyncio
async def test_two_doors_naming_two_people_is_refused(db, cutter, paster):
    """THE DANGEROUS CASE. Silently preferring one would credit the work to
    whichever the server happened to pick — and the wage run pays on that."""
    scanned, card = cutter          # the card physically presented
    other, _ = paster               # a different person entirely

    with pytest.raises(HTTPException) as exc:
        await BarcodeService(db).resolve_actor(
            employee_barcode=card.code, employee_id=other.id)

    assert exc.value.status_code == 422
    detail = str(exc.value.detail)
    # both people are named, so the caller can see which one they meant
    assert scanned.name in detail and other.name in detail
    assert card.code in detail


@pytest.mark.asyncio
async def test_a_barcode_row_id_is_refused_with_the_actual_remedy(db, cutter):
    """THE REPORTED BUG, exactly: an id copied from barcode_registry.

    It is a real UUID and it resolves to nothing, so the old message was just
    "Employee not found." — true, and useless. The caller needs to be told that
    the id came from the wrong table.
    """
    emp, card = cutter
    with pytest.raises(HTTPException) as exc:
        await BarcodeService(db).resolve_actor(
            employee_barcode=None, employee_id=card.id)   # the BARCODE row's id

    assert exc.value.status_code == 404
    detail = str(exc.value.detail)
    assert "employee.id" in detail
    assert "barcode row" in detail
    assert "GET /employees" in detail


@pytest.mark.asyncio
async def test_a_barcode_row_id_alongside_its_own_card_is_still_caught(db, cutter):
    """The precise payload from the report: a valid card plus a barcode-row id.
    The disagreement check fires first and names the card's real owner."""
    emp, card = cutter
    with pytest.raises(HTTPException) as exc:
        await BarcodeService(db).resolve_actor(
            employee_barcode=card.code, employee_id=card.id)
    assert exc.value.status_code == 422
    assert emp.name in str(exc.value.detail)


@pytest.mark.asyncio
async def test_neither_door_is_422(db):
    with pytest.raises(HTTPException) as exc:
        await BarcodeService(db).resolve_actor(
            employee_barcode=None, employee_id=None)
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_an_unknown_card_is_404(db):
    with pytest.raises(HTTPException) as exc:
        await BarcodeService(db).resolve_actor(
            employee_barcode="EMP-NOPE", employee_id=None)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_a_random_uuid_is_404(db):
    with pytest.raises(HTTPException) as exc:
        await BarcodeService(db).resolve_actor(
            employee_barcode=None, employee_id=uuid.uuid4())
    assert exc.value.status_code == 404
