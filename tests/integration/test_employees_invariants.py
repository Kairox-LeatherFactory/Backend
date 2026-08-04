"""
INTEGRATION · the two invariants the employees module exists to hold, plus the
create-time side effects that must be atomic with the row.

FROM THE MODULE'S OWN DOCSTRING (employees/service.py:5-19)
  1. DESIGNATION IS ALWAYS UPPERCASE. Not cosmetic: production's skill gate is a
     set-membership test on the designation, so three spellings of one job title
     means three skills and no gate at all.
  2. NAME IS UNIQUE, disambiguated with an IN-CHAL prefix. Two RAMESH rows are a
     wage misattribution waiting to happen — a manager picks the wrong one from a
     dropdown and the piece money lands in the wrong envelope. The EXISTING row
     is never renamed: it is already printed on wage slips and referenced in
     closed runs.
"""
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.core.enums import BarcodeStatus, BarcodeType, WageType
from app.modules.barcode.models import BarcodeRegistry
from app.modules.employees import schemas
from app.modules.employees.models import Employee
from app.modules.employees.service import EmployeeService
from app.modules.production.service import ProductionService
from app.core.enums import ProductionStage

pytestmark = pytest.mark.integrity


# ═══════════════════════════════════════════ invariant 1 · UPPERCASE designation
@pytest.mark.asyncio
@pytest.mark.parametrize("typed", [
    "shell tailor", "Shell-Tailor", "  SHELL TAILOR  ", "shell_tailor",
    "Shell   Tailor",
])
async def test_however_it_is_typed_a_designation_normalises_to_one_token(db, typed):
    out = await EmployeeService(db).create(schemas.EmployeeCreate(
        name=f"WORKER {typed}", designation=typed, wage_type=WageType.PIECE_RATE))
    assert out.designation == "SHELL_TAILOR"


@pytest.mark.asyncio
async def test_a_sloppily_typed_designation_still_arms_the_skill_gate(db):
    """The consequence, not just the string. A designation stored as
    'shell tailor' would not match STAGE_DESIGNATIONS' 'SHELL_TAILOR' and the
    gate would fail OPEN — the worker could log any stage."""
    out = await EmployeeService(db).create(schemas.EmployeeCreate(
        name="SLOPPY", designation="shell tailor", wage_type=WageType.PIECE_RATE))

    ok = ProductionService._skill_ok(out.designation,
                                     ProductionStage.SHELL_STITCHING)
    blocked = ProductionService._skill_ok(out.designation,
                                          ProductionStage.LEATHER_CUTTING)
    assert ok is True, "a normalised SHELL_TAILOR cannot work shell stitching"
    assert blocked is False, (
        "the skill gate failed open — a shell tailor was allowed to cut leather")


# ══════════════════════════════════════════════ invariant 2 · unique names
@pytest.mark.asyncio
async def test_a_second_ramesh_is_prefixed_and_the_first_is_left_alone(db):
    svc = EmployeeService(db)
    first = await svc.create(schemas.EmployeeCreate(
        name="RAMESH", designation="CUTTER", wage_type=WageType.PIECE_RATE))
    second = await svc.create(schemas.EmployeeCreate(
        name="RAMESH", designation="CUTTER", wage_type=WageType.PIECE_RATE))

    assert first.name == "RAMESH"
    assert second.name == "IN-CHAL RAMESH"

    # the original row is untouched — it is already on printed wage slips
    still = await db.get(Employee, first.id)
    assert still.name == "RAMESH"


@pytest.mark.asyncio
async def test_a_third_and_fourth_collision_keep_counting(db):
    svc = EmployeeService(db)
    names = []
    for _ in range(4):
        out = await svc.create(schemas.EmployeeCreate(
            name="RAMESH", designation="CUTTER", wage_type=WageType.PIECE_RATE))
        names.append(out.name)
    assert names == ["RAMESH", "IN-CHAL RAMESH", "IN-CHAL-2 RAMESH",
                     "IN-CHAL-3 RAMESH"]
    assert len(set(names)) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["ramesh", "Ramesh ", "  RAMESH", "RaMeSh"])
async def test_collision_detection_ignores_case_and_whitespace(db, variant):
    """'ramesh' and 'Ramesh ' are the same person to everyone except a database
    (employees/service.py:66-67)."""
    svc = EmployeeService(db)
    await svc.create(schemas.EmployeeCreate(
        name="RAMESH", designation="CUTTER", wage_type=WageType.PIECE_RATE))
    second = await svc.create(schemas.EmployeeCreate(
        name=variant, designation="CUTTER", wage_type=WageType.PIECE_RATE))
    assert second.name.startswith("IN-CHAL"), (
        f"{variant!r} was not recognised as a collision with RAMESH")


@pytest.mark.asyncio
@pytest.mark.parametrize("blank", ["", "   ", "\t"])
async def test_a_blank_name_is_refused(db, blank):
    with pytest.raises(HTTPException) as exc:
        await EmployeeService(db).create(schemas.EmployeeCreate(
            name=blank, designation="CUTTER", wage_type=WageType.PIECE_RATE))
    assert exc.value.status_code == 422


# ══════════════════════════════════ create-time side effects are atomic
@pytest.mark.asyncio
async def test_every_new_employee_leaves_with_a_scannable_card(db):
    """CLAUDE.md §10: 'every new employee gets an employee barcode (issued in the
    same transaction; the code is returned so the card can be printed)'. An
    employee with no card cannot check in, so cannot log production."""
    out = await EmployeeService(db).create(schemas.EmployeeCreate(
        name="CARDHOLDER", designation="CUTTER", wage_type=WageType.PIECE_RATE))

    assert out.employee_barcode, "no barcode returned — the card cannot be printed"
    row = (await db.execute(select(BarcodeRegistry).where(
        BarcodeRegistry.code == out.employee_barcode))).scalars().first()
    assert row is not None, "the returned code is not in the registry"
    assert row.type == BarcodeType.EMPLOYEE.value
    assert row.status == BarcodeStatus.ACTIVE.value
    assert row.employee_id == out.id


@pytest.mark.asyncio
async def test_a_piece_rate_worker_gets_no_login(db):
    """A daily-wage worker holds no login — the supervisor proxies their
    attendance (CLAUDE.md §10). Provisioning one anyway would create an unused
    credential for every casual worker the factory ever hires."""
    out = await EmployeeService(db).create(schemas.EmployeeCreate(
        name="DAILYWAGE", designation="CUTTER", wage_type=WageType.PIECE_RATE))
    assert out.user_created is False
    assert out.login_phone is None


@pytest.mark.asyncio
async def test_a_monthly_employee_gets_a_login(db):
    out = await EmployeeService(db).create(schemas.EmployeeCreate(
        name="SALARIED ONE", designation="TAILOR", wage_type=WageType.MONTHLY,
        phone="9000000123", password="a-long-enough-password"))
    assert out.user_created is True
    assert out.login_phone == "9000000123"


@pytest.mark.asyncio
async def test_a_monthly_employee_without_login_details_is_refused(db):
    """`EmployeeCreate._login_fields` (employees/schemas.py:46-53) — a MONTHLY
    worker whose login could not be created would be a salaried employee who
    cannot check in."""
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        schemas.EmployeeCreate(name="NOPHONE", designation="TAILOR",
                               wage_type=WageType.MONTHLY)


@pytest.mark.asyncio
async def test_a_password_is_refused_for_a_piece_rate_worker(db):
    """The inverse guard: a password supplied for someone who gets no login is a
    credential that would silently go nowhere."""
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        schemas.EmployeeCreate(name="ODD", designation="CUTTER",
                               wage_type=WageType.PIECE_RATE,
                               phone="9000000999", password="something-long")


# ═══════════════════════════════════ F47 · the update allow-list (mass assignment)
@pytest.mark.asyncio
async def test_update_rejects_a_field_outside_the_allow_list(db):
    """F47 (employees/service.py:146-156). The old code mass-setattr'd whatever
    arrived. `EmployeeUpdate` carries `is_active` and `monthly_salary`, so the
    moment a PATCH route is added that becomes a live payroll and privilege
    write.

    Reaching the guard needs a body whose `model_dump` yields a key the
    allow-list does not name. A real `EmployeeUpdate` cannot produce one —
    Pydantic dumps declared fields only — so a stand-in is used. That is not
    contrivance: the guard's whole purpose is to hold if the SCHEMA later grows a
    field nobody re-checked against the allow-list, and this is what that looks
    like from the service's side.
    """
    out = await EmployeeService(db).create(schemas.EmployeeCreate(
        name="UPDATABLE", designation="CUTTER", wage_type=WageType.PIECE_RATE))

    class WidenedUpdate:
        """A future EmployeeUpdate that gained a field the allow-list forgot."""
        def model_dump(self, **kw):
            return {"designation": "paster", "is_superuser": True}

    with pytest.raises(HTTPException) as exc:
        await EmployeeService(db).update(out.id, WidenedUpdate())
    assert exc.value.status_code == 422
    assert "not updatable" in str(exc.value.detail).lower()
    assert "is_superuser" in str(exc.value.detail)

    # and nothing was written before the refusal
    await db.refresh(out_row := await db.get(Employee, out.id))
    assert out_row.designation == "CUTTER"


def test_the_allow_list_covers_every_field_the_update_schema_declares():
    """The other failure mode of an allow-list: a field the schema OFFERS but the
    list omits is silently dropped, so a manager's edit appears to save and does
    nothing. Asserted as a pure structural check — no DB needed."""
    allowed = {"name", "designation", "wage_type", "monthly_salary",
               "phone", "email", "is_active"}
    declared = set(schemas.EmployeeUpdate.model_fields)
    assert declared == allowed, (
        f"schema and allow-list disagree: only-in-schema={declared - allowed}, "
        f"only-in-list={allowed - declared}")


@pytest.mark.asyncio
async def test_a_legitimate_update_still_normalises_the_designation(db):
    out = await EmployeeService(db).create(schemas.EmployeeCreate(
        name="PROMOTED", designation="CUTTER", wage_type=WageType.PIECE_RATE))

    updated = await EmployeeService(db).update(
        out.id, schemas.EmployeeUpdate(designation="line tailor"))
    assert updated.designation == "LINE_TAILOR"


@pytest.mark.asyncio
async def test_updating_a_missing_employee_is_a_404(db):
    import uuid
    with pytest.raises(HTTPException) as exc:
        await EmployeeService(db).update(
            uuid.uuid4(), schemas.EmployeeUpdate(designation="CUTTER"))
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_renaming_onto_an_existing_name_is_disambiguated_too(db):
    """A rename is a collision risk exactly like a create
    (employees/service.py:142-143)."""
    svc = EmployeeService(db)
    await svc.create(schemas.EmployeeCreate(
        name="TAKEN", designation="CUTTER", wage_type=WageType.PIECE_RATE))
    other = await svc.create(schemas.EmployeeCreate(
        name="ORIGINAL", designation="CUTTER", wage_type=WageType.PIECE_RATE))

    updated = await EmployeeService(db).update(
        other.id, schemas.EmployeeUpdate(name="TAKEN"))
    assert updated.name == "IN-CHAL TAKEN"

    n = await db.scalar(select(func.count(Employee.id))
                        .where(func.lower(Employee.name) == "taken"))
    assert n == 1, "two employees now share one name"
