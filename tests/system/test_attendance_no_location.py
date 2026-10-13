"""
SYSTEM / E2E · check-in and check-out with LOCATION TRACKING REMOVED.

The factory no longer holds a coordinate and no longer asks a device for one.
This layer is where that has to be proven, because the old contract was enforced
by Pydantic at the REQUEST BOUNDARY: `CheckInRequest` extended a `GpsPoint` whose
lat/lon were required, so a body-less POST was rejected by FastAPI before any
service code ran. A service-level test cannot see that at all.

Three properties, one test each:
  1. an EMPTY body checks in — the point of the change;
  2. a body that still carries lat/lon is ACCEPTED and IGNORED — the promise to a
     frontend that has not been redeployed, including coordinates that used to be
     far outside the fence and would have been a 403;
  3. the config endpoints neither return nor accept the fence geometry.
"""
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.database import get_db
from app.core.enums import UserRole, WageType
from app.main import app
from app.modules.employees.models import Employee
from app.modules.users.deps import get_current_user

API = "/api/v1"

# ~3 km from the Bengaluru coordinates the fence tests used — a hard 403 before.
FAR_LAT, FAR_LON = 12.9900, 77.6200


class FakeUser:
    def __init__(self, role, employee_id=None):
        self.id = uuid.uuid4()
        self.role = role
        self.name = role.value
        self.phone = "9000000000"
        self.employee_id = employee_id
        self.client_id = None
        self.is_active = True


@pytest_asyncio.fixture
async def client(db):
    async def _db():
        yield db
    app.dependency_overrides[get_db] = _db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _operator(db, name="GATE GUARD"):
    """A SECURITY login linked to an employee row — the gate operator whose own
    arrival goes through /attendance/check-in."""
    emp = Employee(name=name, designation="SECURITY",
                   wage_type=WageType.MONTHLY, is_active=True)
    db.add(emp)
    await db.commit()
    await db.refresh(emp)
    user = FakeUser(UserRole.SECURITY, employee_id=emp.id)
    app.dependency_overrides[get_current_user] = lambda: user
    return emp


@pytest.mark.asyncio
async def test_check_in_with_no_body_at_all(client, db):
    """THE HEADLINE. No factory coordinate is configured, none is sent, and the
    punch lands. This POST was a 422 before the removal."""
    await _operator(db)

    r = await client.post(f"{API}/attendance/check-in")

    assert r.status_code == 201, r.text
    assert r.json()["distance_m"] is None


@pytest.mark.asyncio
async def test_check_in_and_out_with_an_empty_json_body(client, db):
    """The other shape a frontend sends when it has nothing to say."""
    await _operator(db, name="EMPTY BODY GUARD")

    assert (await client.post(f"{API}/attendance/check-in",
                              json={})).status_code == 201
    out = await client.post(f"{API}/attendance/check-out", json={})
    assert out.status_code == 200, out.text
    assert out.json()["check_out_at"] is not None


@pytest.mark.asyncio
async def test_coordinates_are_accepted_and_ignored(client, db):
    """COMPATIBILITY. An un-redeployed frontend still posts its GPS. It must not
    422 on the extra keys — and coordinates that used to be a 403 (3 km off site,
    against a fence that no longer exists) must now simply be ignored."""
    await _operator(db, name="LEGACY CLIENT GUARD")

    r = await client.post(f"{API}/attendance/check-in",
                          json={"lat": FAR_LAT, "lon": FAR_LON})

    assert r.status_code == 201, r.text
    assert r.json()["distance_m"] is None, "a sent coordinate must not be stored"


@pytest.mark.asyncio
async def test_the_manual_door_needs_only_the_employee_ids(client, db):
    """`ProxyMarkRequest.lat/lon` were required — the manual fallback could not
    be used at all without the operator's position."""
    emp = Employee(name="FLOOR WORKER", designation="CUTTER",
                   wage_type=WageType.PIECE_RATE, is_active=True)
    db.add(emp)
    await db.commit()
    await db.refresh(emp)
    await _operator(db, name="MANUAL DOOR GUARD")

    r = await client.post(f"{API}/attendance/proxy/check-in",
                          json={"employee_ids": [str(emp.id)]})

    assert r.status_code == 201, r.text
    assert r.json()[0]["distance_m"] is None


@pytest.mark.asyncio
async def test_the_config_endpoints_are_withdrawn(client, db):
    """UPDATED 2026-09-19 (Hamthan): GET and PATCH /attendance/config are gone
    — commented out in attendance/router.py with the reasoning. This asserted
    they carried no fence geometry; the strongest version of that now is that
    there is no endpoint to carry any.

    Both are checked, not just the read: leaving the WRITE door open without
    the read door is the dangerous half, because an edit form that cannot load
    current policy saves whatever the frontend happened to be holding."""
    from fastapi.routing import APIRoute

    live = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert f"{API}/attendance/config" not in live

    # And over HTTP. NOT 404: `/attendance/{attendance_id}` (the punch
    # correction routes) is a literal-free path that still matches the string
    # "config", so the server answers 405 on GET and 422 on PATCH — the UUID
    # never parses. Either way there is no handler left to run.
    await _operator(db, name="CONFIG GUARD")
    assert (await client.get(f"{API}/attendance/config")).status_code != 200

    app.dependency_overrides[get_current_user] = lambda: FakeUser(UserRole.HR)
    patched = await client.patch(f"{API}/attendance/config",
                                 json={"shift_start": "08:30"})
    assert patched.status_code != 200, patched.text


@pytest.mark.asyncio
async def test_the_shift_config_contract_still_carries_no_fence_geometry():
    """The guarantee the test above used to make, kept where it still applies.

    The shift POLICY did not go anywhere — the service reads it on every punch
    to set is_late / is_short / is_overtime — so its schemas are still live and
    must stay free of fence coordinates. If the endpoints are ever restored,
    they cannot bring the geofence back with them."""
    from app.modules.attendance import schemas as att_schemas

    for shape in (att_schemas.ShiftConfigRead, att_schemas.ShiftConfigPublicRead,
                  att_schemas.ShiftConfigUpdate):
        for gone in ("factory_lat", "factory_lon", "radius_m"):
            assert gone not in shape.model_fields, f"{shape.__name__}.{gone}"
