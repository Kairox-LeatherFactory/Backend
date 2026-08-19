"""
INTEGRATION · attendance integrity — the gate in front of production and wages.

Priority-2 coverage. Attendance is not a reporting nicety here: `is_present_today`
gates ALL production logging (production/service.py:176-182), and days-present can
scale monthly pay (wages/service.py:490-495). A fabricated or missing attendance
row moves both output records and money.

Covers: work_date uniqueness, idempotent re-tap, the proxy wage-type
restriction, and — since LOCATION TRACKING WAS REMOVED — that a punch needs
no factory position and no device position to succeed. The geofence tests it
used to carry are commented out in place, next to what replaced them.
"""
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.core.enums import UserRole, WageType
from app.modules.attendance import schemas
from app.modules.attendance.models import AttendanceLog, AttendanceSource, ShiftConfig
from app.modules.attendance.service import AttendanceService
from app.modules.employees.models import Employee

# LOCATION REMOVED — kept only because the tests below still pass coordinates
# on the wire to prove they are ACCEPTED AND IGNORED, which is the compatibility
# promise made to a frontend that has not been redeployed yet.
FACTORY_LAT, FACTORY_LON = 12.9716, 77.5946      # Bengaluru
NEAR = (12.97165, 77.59465)                       # ~7 m away
FAR = (12.9900, 77.6200)                          # ~3 km — no longer refused


@pytest.fixture
def worker_user():
    class U:
        id = uuid.uuid4()
        role = UserRole.EMPLOYEE
        name = "WORKER"
        employee_id = None
        client_id = None
    return U()


@pytest.fixture
def supervisor():
    class U:
        id = uuid.uuid4()
        role = UserRole.SUPERVISOR
        name = "SUPE"
        employee_id = None
        client_id = None
    return U()


async def _configured(db):
    """Materialise the ShiftConfig singleton.

    It used to seed REAL factory coordinates, because without them the fence sat
    at 0N 0E and refused every genuine check-in (the Null Island finding in
    pass-12). LOCATION REMOVED — no coordinates are set, and that is precisely
    the point: check-in works with the config untouched.

        cfg.factory_lat = FACTORY_LAT
        cfg.factory_lon = FACTORY_LON
        cfg.radius_m = 100
    """
    cfg = await AttendanceService(db)._config()
    await db.commit()
    return cfg


async def _employee(db, name="RAMESH", wage_type=WageType.PIECE_RATE):
    emp = Employee(name=name, designation="CUTTER", wage_type=wage_type,
                   is_active=True)
    db.add(emp)
    await db.commit()
    await db.refresh(emp)
    return emp


# ══════════════════════════════════════════════════════ one row per day
@pytest.mark.asyncio
async def test_work_date_uniqueness_is_enforced_by_the_database(db):
    """`uq_att_emp_day` (attendance/models.py:71-72) is what makes 'one attendance
    per day' true even under a race — the service's read-then-insert alone cannot."""
    from sqlalchemy.exc import IntegrityError

    emp = await _employee(db)
    emp_id, today = emp.id, date.today()

    db.add(AttendanceLog(employee_id=emp_id, work_date=today,
                         check_in_at=datetime.now(timezone.utc),
                         source=AttendanceSource.SELF))
    await db.commit()

    db.add(AttendanceLog(employee_id=emp_id, work_date=today,
                         check_in_at=datetime.now(timezone.utc),
                         source=AttendanceSource.PROXY))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()

    n = await db.scalar(select(func.count(AttendanceLog.id))
                        .where(AttendanceLog.employee_id == emp_id))
    assert n == 1


@pytest.mark.asyncio
async def test_the_same_worker_may_be_present_on_two_different_days(db):
    emp = await _employee(db)
    for offset in (0, 1):
        db.add(AttendanceLog(employee_id=emp.id,
                             work_date=date.today() - timedelta(days=offset),
                             check_in_at=datetime.now(timezone.utc),
                             source=AttendanceSource.SELF))
    await db.commit()
    n = await db.scalar(select(func.count(AttendanceLog.id))
                        .where(AttendanceLog.employee_id == emp.id))
    assert n == 2


@pytest.mark.asyncio
async def test_re_tapping_the_card_is_a_no_op(db, worker_user):
    """A worker who scans twice at the gate must not create a second row, and must
    not have their check-in time moved. The service catches the IntegrityError and
    returns the existing row (service.py:309-316)."""
    await _configured(db)
    emp = await _employee(db)
    worker_user.employee_id = emp.id
    svc = AttendanceService(db)

    first = await svc.self_check_in(
        worker_user, schemas.CheckInRequest(lat=NEAR[0], lon=NEAR[1]))
    original = first.check_in_at

    second = await svc.self_check_in(
        worker_user, schemas.CheckInRequest(lat=NEAR[0], lon=NEAR[1]))

    assert second.id == first.id
    assert second.check_in_at == original
    n = await db.scalar(select(func.count(AttendanceLog.id))
                        .where(AttendanceLog.employee_id == emp.id))
    assert n == 1


# ════════════════════════════════════════════ LOCATION REMOVED (was: geofence)
@pytest.mark.asyncio
async def test_a_worker_checks_in_with_no_location_at_all(db, worker_user):
    """The headline of this change: an empty body is a valid check-in.

    No factory coordinate is configured (see `_configured`) and none is sent, and
    the punch still lands — identity comes from the token, the timestamp from the
    server. `distance_m` stays NULL because nothing measures a distance now."""
    await _configured(db)
    emp = await _employee(db)
    worker_user.employee_id = emp.id

    log = await AttendanceService(db).self_check_in(worker_user)

    assert log.employee_id == emp.id
    assert log.source == AttendanceSource.SELF
    assert log.distance_m is None


@pytest.mark.asyncio
async def test_coordinates_are_accepted_and_ignored(db, worker_user):
    """The compatibility promise: a frontend that still posts its GPS is not
    broken by the removal — and coordinates 3 km off site no longer refuse the
    punch, because nothing looks at them."""
    await _configured(db)
    emp = await _employee(db)
    worker_user.employee_id = emp.id

    log = await AttendanceService(db).self_check_in(
        worker_user, schemas.CheckInRequest(lat=FAR[0], lon=FAR[1]))

    assert log.employee_id == emp.id
    assert log.distance_m is None, "a sent coordinate must not be recorded"


@pytest.mark.asyncio
async def test_a_login_with_no_employee_record_cannot_check_itself_in(db, worker_user):
    """The one check that survives on the SELF door: you must BE somebody."""
    await _configured(db)
    worker_user.employee_id = None
    with pytest.raises(HTTPException) as exc:
        await AttendanceService(db).self_check_in(worker_user)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_self_check_in_needs_no_gps():
    """Inversion of `test_self_check_in_always_carries_gps`.

    `CheckInRequest` used to extend a `GpsPoint` whose lat/lon were REQUIRED, so
    constructing one without coordinates raised. Now it must construct cleanly —
    that is what lets the router accept an absent body."""
    body = schemas.CheckInRequest()
    assert body.lat is None and body.lon is None


@pytest.mark.asyncio
async def test_a_barcode_scan_carries_no_position(db, supervisor):
    """The barcode door — THE primary attendance surface — with no coordinates
    and no `reason`. This used to be the audited bypass (pass-03 top-10 #7),
    tolerated only with a free-text excuse. It is now simply how scanning works:
    the operator's login and the card are the whole authorisation."""
    await _configured(db)
    emp = await _employee(db)

    log = await AttendanceService(db).barcode_scan(
        employee_id=emp.id, actor=supervisor, direction="in", proxy=False,
        lat=None, lon=None, reason=None)

    assert log["present_today"] is True
    row = await db.scalar(select(AttendanceLog)
                          .where(AttendanceLog.employee_id == emp.id))
    assert row.distance_m is None
    assert await AttendanceService(db).is_present_today(emp.id) is True


@pytest.mark.asyncio
async def test_a_proxy_scan_needs_no_position_either(db, supervisor):
    """A proxy scan used to be the one path that could NOT skip GPS (a supervisor
    on the floor was assumed to have a fix). It no longer needs one."""
    await _configured(db)
    emp = await _employee(db)

    log = await AttendanceService(db).barcode_scan(
        employee_id=emp.id, actor=supervisor, direction="in",
        lat=None, lon=None, proxy=True, reason=None)

    assert log["present_today"] is True


# ── the geofence tests these replaced ───────────────────────────────────────
# Commented out, not deleted: they are the coverage that comes back with
# app/modules/attendance/geofence.py if the 100-metre rule is ever restored.
#
# # ══════════════════════════════════════════════════════════════ geofence
# @pytest.mark.asyncio
# async def test_a_worker_at_the_factory_may_check_in(db, worker_user):
#     await _configured(db)
#     emp = await _employee(db)
#     worker_user.employee_id = emp.id
#
#     log = await AttendanceService(db).self_check_in(
#         worker_user, schemas.CheckInRequest(lat=NEAR[0], lon=NEAR[1]))
#
#     assert log.employee_id == emp.id
#     assert log.source == AttendanceSource.SELF
#     assert log.distance_m is not None
#
#
# @pytest.mark.asyncio
# async def test_a_worker_off_site_is_refused(db, worker_user):
#     await _configured(db)
#     emp = await _employee(db)
#     worker_user.employee_id = emp.id
#
#     with pytest.raises(HTTPException) as exc:
#         await AttendanceService(db).self_check_in(
#             worker_user, schemas.CheckInRequest(lat=FAR[0], lon=FAR[1]))
#     assert exc.value.status_code == 403
#     assert "meters" in str(exc.value.detail)
#
#     n = await db.scalar(select(func.count(AttendanceLog.id)))
#     assert n == 0, "a refused check-in must not leave a row"
#
#
# @pytest.mark.asyncio
# async def test_a_login_with_no_employee_record_cannot_check_itself_in(db, worker_user):
#     await _configured(db)
#     worker_user.employee_id = None
#     with pytest.raises(HTTPException) as exc:
#         await AttendanceService(db).self_check_in(
#             worker_user, schemas.CheckInRequest(lat=NEAR[0], lon=NEAR[1]))
#     assert exc.value.status_code == 400
#
#
# @pytest.mark.asyncio
# async def test_self_check_in_always_carries_gps(db):
#     """`CheckInRequest` extends `GpsPoint` (schemas.py:24-36), so lat/lon are
#     mandatory on the SELF door and the fence can never be skipped there. The
#     barcode door is the one that makes them optional — see below."""
#     with pytest.raises(Exception):
#         schemas.CheckInRequest()
#
#
# # ═══════════════════════════════════ the GPS-optional bypass (AUDIT finding)
# @pytest.mark.asyncio
# async def test_a_gps_less_barcode_scan_skips_the_fence_entirely(db, supervisor):
#     """AUDIT (pass-03-security.md, top-10 #7): omit lat/lon on the barcode door and
#     `_enforce_geofence` never runs (attendance/service.py:195-207). The only cost is
#     a free-text `reason` that nothing validates — and the resulting row still
#     satisfies `is_present_today`, which gates production logging.
#
#     This test pins the CURRENT behaviour deliberately. When the finding is fixed it
#     will fail, and that failure is the signal the fix landed. Do not delete it —
#     invert it.
#     """
#     await _configured(db)
#     emp = await _employee(db)
#
#     log = await AttendanceService(db).barcode_scan(
#         employee_id=emp.id, actor=supervisor, direction="in", proxy=False,
#         lat=None, lon=None, reason="indoor, no fix")
#
#     assert log["location_unverified"] is True, "no distance was ever measured"
#     assert log["present_today"] is True
#
#     row = await db.scalar(select(AttendanceLog)
#                           .where(AttendanceLog.employee_id == emp.id))
#     assert row.distance_m is None, "the row records no position at all"
#     assert await AttendanceService(db).is_present_today(emp.id) is True, (
#         "an unverified row still counts as present — this is the finding")
#
#
# @pytest.mark.asyncio
# async def test_a_gps_less_scan_at_least_demands_a_reason(db, supervisor):
#     """The one control that does exist on that path (service.py:203-206)."""
#     await _configured(db)
#     emp = await _employee(db)
#
#     with pytest.raises(HTTPException) as exc:
#         await AttendanceService(db).barcode_scan(
#             employee_id=emp.id, actor=supervisor, direction="in", proxy=False,
#             lat=None, lon=None, reason="")
#     assert exc.value.status_code == 422
#
#
# @pytest.mark.asyncio
# async def test_a_proxy_scan_must_carry_the_supervisors_gps(db, supervisor):
#     """A supervisor standing on the floor has a device with a fix; the reason
#     escape hatch is not available to them (service.py:199-202)."""
#     await _configured(db)
#     emp = await _employee(db)
#
#     with pytest.raises(HTTPException) as exc:
#         await AttendanceService(db).barcode_scan(
#             employee_id=emp.id, actor=supervisor, direction="in",
#             lat=None, lon=None, proxy=True, reason="no fix")
#     assert exc.value.status_code == 422


# ═══════════════════════════════════════════════════════ proxy restrictions
@pytest.mark.asyncio
async def test_proxy_marking_is_limited_to_piece_rate_workers(db, supervisor):
    """Proxy exists for daily-wage workers who hold no login. A monthly employee
    has one, so marking them by proxy is refused (service.py:245-247)."""
    await _configured(db)
    salaried = await _employee(db, name="SALARIED", wage_type=WageType.MONTHLY)

    with pytest.raises(HTTPException) as exc:
        await AttendanceService(db).proxy_mark_present(
            supervisor, schemas.ProxyMarkRequest(
                employee_ids=[salaried.id], lat=NEAR[0], lon=NEAR[1]))
    assert exc.value.status_code == 400
    assert "piece-rate" in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_proxy_marking_a_piece_rate_worker_succeeds(db, supervisor):
    await _configured(db)
    emp = await _employee(db)

    out = await AttendanceService(db).proxy_mark_present(
        supervisor, schemas.ProxyMarkRequest(
            employee_ids=[emp.id], lat=NEAR[0], lon=NEAR[1]))

    assert len(out) == 1
    assert out[0].source == AttendanceSource.PROXY
    assert out[0].recorded_by_user_id == supervisor.id


# LOCATION REMOVED — the operator's own position is no longer a precondition
# for marking anyone present, so an "off-site supervisor" is not a thing the
# system can detect or refuse.
#
# @pytest.mark.asyncio
# async def test_a_supervisor_off_site_cannot_mark_anyone_present(db, supervisor):
#     await _configured(db)
#     emp = await _employee(db)
#
#     with pytest.raises(HTTPException) as exc:
#         await AttendanceService(db).proxy_mark_present(
#             supervisor, schemas.ProxyMarkRequest(
#                 employee_ids=[emp.id], lat=FAR[0], lon=FAR[1]))
#     assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_proxy_marking_needs_only_the_employee_ids(db, supervisor):
    """Replacement for the off-site test: `ProxyMarkRequest` is valid with no
    coordinates, which is what the manual door now posts."""
    await _configured(db)
    emp = await _employee(db, name="MANUAL")

    out = await AttendanceService(db).proxy_mark_present(
        supervisor, schemas.ProxyMarkRequest(employee_ids=[emp.id]))

    assert len(out) == 1 and out[0].distance_m is None


# ═══════════════════════════════════════════ the bridge into production/wages
@pytest.mark.asyncio
async def test_is_present_today_is_false_before_any_scan(db):
    emp = await _employee(db)
    assert await AttendanceService(db).is_present_today(emp.id) is False


@pytest.mark.asyncio
async def test_days_present_counts_only_days_inside_the_window(db):
    """`days_present` feeds monthly proration when the (currently dead) setting
    is enabled — see pass-02. Its window arithmetic still has to be right."""
    emp = await _employee(db)
    base = date.today()
    for offset in (0, 1, 2, 10):
        db.add(AttendanceLog(employee_id=emp.id, work_date=base - timedelta(days=offset),
                             check_in_at=datetime.now(timezone.utc),
                             source=AttendanceSource.SELF))
    await db.commit()

    got = await AttendanceService(db).days_present(
        emp.id, base - timedelta(days=2), base)
    assert got == 3
@pytest.mark.asyncio
async def test_security_scans_monthly_employee(db, security_user, monthly_card):
    from app.modules.attendance.service import AttendanceService
    res = await AttendanceService(db).barcode_scan(
        employee_id=monthly_card.employee_id, actor=security_user,
        direction="in", lat=12.9, lon=80.2, proxy=False, reason=None)
    assert res["present_today"] is True
 
@pytest.mark.asyncio
async def test_manual_checkin_works_for_monthly_worker(db, security_user, monthly_emp):
    """A MONTHLY worker forgot their card → SECURITY manual check-in. This used to
    raise 400 ('not a piece-rate worker'); now it must succeed."""
    from app.modules.attendance.service import AttendanceService
    from app.modules.attendance.schemas import ProxyMarkRequest
    out = await AttendanceService(db).proxy_mark_present(
        security_user, ProxyMarkRequest(
            employee_ids=[monthly_emp.id], lat=12.9, lon=80.2))
    assert len(out) == 1
 
@pytest.mark.asyncio
async def test_hr_md_dm_can_manual_checkin(client, hr_token, md_token, dm_token, emp_id):
    for tok in (hr_token, md_token, dm_token):
        r = await client.post("/api/v1/attendance/proxy/check-in",
            headers={"Authorization": f"Bearer {tok}"},
            json={"employee_ids": [str(emp_id)], "lat": 12.9, "lon": 80.2})
        assert r.status_code in (200, 201)
 
@pytest.mark.asyncio
async def test_employee_cannot_manual_checkin(client, employee_token, other_emp_id):
    r = await client.post("/api/v1/attendance/proxy/check-in",
        headers={"Authorization": f"Bearer {employee_token}"},
        json={"employee_ids": [str(other_emp_id)], "lat": 12.9, "lon": 80.2})
    assert r.status_code == 403
 
@pytest.mark.asyncio
async def test_security_cannot_create_employee(client, security_token):
    r = await client.post("/api/v1/employees",
        headers={"Authorization": f"Bearer {security_token}"},
        json={"name": "X", "wage_type": "piece_rate"})
    assert r.status_code == 403
    
if __name__ == "__main__":
    print("Item 1 FINAL — unified: everyone has a card; SECURITY/HR/MD/DM scan or "
          "manual-check-in anyone, any wage type; employee creation stays HR/MD/DM.")