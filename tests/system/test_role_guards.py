"""
SYSTEM / E2E · authorization through the real FastAPI app.

Router -> dependency -> service, over HTTP via httpx ASGITransport. What is being
asserted here is the STATUS CODE and who may reach an endpoint — not business
results, which the integration layer already covers.

Why this layer exists separately: every role gate in this codebase is a
`Depends(...)`, and a dependency that is declared but mis-ordered, shadowed by an
earlier path, or attached to the wrong router is invisible to a service-level
test. The only way to see it is to speak HTTP.

`get_current_user` is overridden rather than minting real JWTs: the token path has
its own coverage, and overriding keeps these tests about authorization instead of
about signing.
"""
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.database import get_db
from app.core.enums import UserRole
from app.main import app
from app.modules.users.deps import get_current_user

API = "/api/v1"


class FakeUser:
    """Duck-typed stand-in for the User ORM row the deps return."""
    def __init__(self, role, employee_id=None, client_id=None):
        self.id = uuid.uuid4()
        self.role = role
        self.name = role.value
        self.phone = "9000000000"
        self.employee_id = employee_id
        self.client_id = client_id
        self.is_active = True


@pytest_asyncio.fixture
async def client(db):
    """An HTTP client bound to the app, with the test session and no real auth."""
    async def _db():
        yield db
    app.dependency_overrides[get_db] = _db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def _as(role, **kw):
    """Install `role` as the authenticated caller for the next request."""
    app.dependency_overrides[get_current_user] = lambda: FakeUser(role, **kw)


# ══════════════════════════════════════════════════════════ unauthenticated
@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    f"{API}/wages/runs",
    f"{API}/employees",
    f"{API}/clients",
    f"{API}/analytics/overview",
    f"{API}/materials/stock",
])
async def test_no_token_is_refused(client, path):
    app.dependency_overrides.pop(get_current_user, None)
    r = await client.get(path)
    assert r.status_code == 401, f"{path} served an anonymous caller"


@pytest.mark.asyncio
async def test_health_is_public(client):
    app.dependency_overrides.pop(get_current_user, None)
    r = await client.get("/health")
    assert r.status_code == 200


# ══════════════════════════════════════ block_employees on manager routers
@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    f"{API}/wages/runs",
    f"{API}/employees",
    f"{API}/clients",
    f"{API}/analytics/overview",
    f"{API}/materials/stock",
    f"{API}/production/operations",
])
async def test_an_employee_token_cannot_reach_manager_routers(client, path):
    """`_LOCKED` (main.py:96) wraps these routers in `block_employees`."""
    _as(UserRole.EMPLOYEE)
    r = await client.get(path)
    assert r.status_code == 403, f"{path} admitted an EMPLOYEE token"


@pytest.mark.asyncio
async def test_an_employee_may_still_resolve_a_barcode(client):
    """Deliberately NOT locked (main.py:246, barcode/router.py:36) — a worker has
    to be able to scan their own card."""
    _as(UserRole.EMPLOYEE)
    r = await client.get(f"{API}/barcode/resolve", params={"code": "NOPE-404"})
    assert r.status_code != 403
    assert r.status_code == 404      # unknown code, not a permission problem


@pytest.mark.asyncio
async def test_an_employee_may_still_read_their_own_attendance(client):
    """`attendance_router` is intentionally open (main.py:241) so workers punch in."""
    _as(UserRole.EMPLOYEE, employee_id=uuid.uuid4())
    r = await client.get(f"{API}/attendance/me")
    assert r.status_code != 403


# ═══════════════════════════════════════════════════ wages: who may see money
@pytest.mark.asyncio
@pytest.mark.parametrize("role", [UserRole.HR, UserRole.DIRECT_MANAGER,
                                  UserRole.MANAGING_DIRECTOR])
async def test_payroll_readers_may_list_runs(client, role):
    _as(role)
    r = await client.get(f"{API}/wages/runs")
    assert r.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [UserRole.CUTTING_MANAGER, UserRole.STITCHING_MANAGER,
                                  UserRole.LINING_MANAGER, UserRole.SUPERVISOR,
                                  UserRole.VIEWER, UserRole.CLIENT])
async def test_everyone_else_is_refused_payroll(client, role):
    """Wages are visible to HR / DM / MD only. A supervisor reading the whole
    factory's pay is exactly the exposure the role list exists to prevent."""
    _as(role)
    r = await client.get(f"{API}/wages/runs")
    assert r.status_code == 403, f"{role.value} read the payroll list"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [UserRole.HR, UserRole.SUPERVISOR,
                                  UserRole.CUTTING_MANAGER, UserRole.VIEWER])
async def test_only_the_direct_manager_may_create_a_run(client, role):
    """Starting payroll is a DM act (wages/router.py:143). HR reads, DM runs."""
    _as(role)
    r = await client.post(f"{API}/wages/runs",
                          json={"period_start": "2026-07-01", "period_end": "2026-07-14"})
    assert r.status_code == 403, f"{role.value} started a payroll run"


# ═════════════════════════════════════════════════ employees: salary exposure
@pytest.mark.asyncio
@pytest.mark.parametrize("role", [UserRole.CLIENT, UserRole.VIEWER])
async def test_the_roster_is_internal(client, role):
    """F37: the employee roster carries phone and email, so redacting salary alone
    was not enough — CLIENT and VIEWER are refused outright
    (employees/router.py:32-35)."""
    _as(role)
    r = await client.get(f"{API}/employees")
    assert r.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("role,expect_pay", [
    (UserRole.HR, True),
    (UserRole.DIRECT_MANAGER, True),
    (UserRole.MANAGING_DIRECTOR, True),
    (UserRole.SUPERVISOR, False),
    (UserRole.CUTTING_MANAGER, False),
])
async def test_salary_is_returned_only_to_hr_dm_md(client, role, expect_pay):
    """F37 in its positive form: the roster is readable by the floor, the SALARY
    column is not (employees/router.py:39-40)."""
    _as(role)
    r = await client.get(f"{API}/employees")
    assert r.status_code == 200
    rows = r.json()
    if rows:
        assert ("monthly_salary" in rows[0]) is expect_pay


# ═══════════════════════════════════════════════════ materials + imports
@pytest.mark.asyncio
@pytest.mark.parametrize("role", [UserRole.SUPERVISOR, UserRole.HR, UserRole.VIEWER])
async def test_only_lot_writers_may_create_a_material_lot(client, role):
    _as(role)
    r = await client.post(f"{API}/materials/lots", json={
        "category": "LEATHER", "article": "A", "colour": "BLACK",
        "attributes": {"thickness": "1.2mm", "dcm": 100}})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_the_lining_manager_cannot_create_lining_lots(client):
    """AUDIT (pass-04-role-permissions.md): `_LOT_WRITERS` is DM/MD/CUTTING_MANAGER
    (materials/router.py:30-31). LINING_MANAGER must supply a `lining_lot_id` at
    LINING_CUTTING (production/service.py:279-290) but cannot create one.

    Pins the current behaviour so the fix is a visible change.
    """
    _as(UserRole.LINING_MANAGER)
    r = await client.post(f"{API}/materials/lots", json={
        "category": "LINING", "subtype": "PLAIN", "article": "A",
        "colour": "BLACK", "attributes": {"thickness": "0.5mm", "mtrs": 50}})
    assert r.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [UserRole.HR, UserRole.SUPERVISOR,
                                  UserRole.CUTTING_MANAGER])
async def test_only_the_direct_manager_may_commit_an_import(client, role):
    """A breakdown upload mints every piece and drawer in the order — DM only
    (imports/router.py:108,124)."""
    _as(role)
    r = await client.post(f"{API}/imports/preview")
    assert r.status_code == 403


# ═══════════════════════════════════════════ analytics: the missing gates
@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    f"{API}/analytics/explorer",
    f"{API}/analytics/alerts/stage-spread",
    f"{API}/analytics/alerts/freight-risk",
    f"{API}/analytics/consumption",
])
async def test_analytics_endpoints_have_no_role_gate(client, path):
    """AUDIT (pass-03-security.md): 7 of 10 analytics endpoints carry no
    `require_roles`. `client_scope` is a TENANCY filter that returns None for every
    non-CLIENT role — it is not an authorization check.

    So a SUPERVISOR reads the full order explorer and the freight-risk alerts.
    This test pins the current behaviour; when the gates are added it fails, and
    that failure is the confirmation. Do not delete it — invert it to 403.
    """
    _as(UserRole.SUPERVISOR)
    r = await client.get(path)
    assert r.status_code == 200, (
        "expected the ungated behaviour recorded in the audit")


@pytest.mark.asyncio
async def test_employee_rates_is_gated(client):
    """The one analytics endpoint that does carry a role list — because it exposes
    pay (analytics/router.py:101-102)."""
    _as(UserRole.SUPERVISOR)
    r = await client.get(f"{API}/analytics/employee-rates",
                         params={"start": "2026-07-01", "end": "2026-07-14"})
    assert r.status_code == 403


# ══════════════════════════════════════ production: the broken tenancy GETs
@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    f"{API}/production/skus",
    f"{API}/production/skus/{uuid.uuid4()}/pieces",
    f"{API}/production/styles/{uuid.uuid4()}/progress",
])
async def test_the_client_scope_gets_are_broken(client, path):
    """AUDIT F140 (BLOCKER, pass-01): the router passes `client_scope=scope`
    (production/router.py:71,98,110) to service methods whose signatures accept no
    such kwarg (service.py:376,435,438). Every call raises TypeError.

    Asserted as a raised TypeError rather than a 500 because ASGITransport
    propagates the exception instead of letting the handler convert it. When the
    signatures are fixed this test fails — that is the point.
    """
    _as(UserRole.DIRECT_MANAGER)
    with pytest.raises(TypeError, match="client_scope"):
        await client.get(path)


@pytest.mark.asyncio
async def test_the_ungated_production_reads_are_still_role_checked(client):
    """`/production/operations` uses `_FLOOR_READERS` and is unaffected."""
    _as(UserRole.CLIENT)
    r = await client.get(f"{API}/production/operations")
    assert r.status_code == 403


# ═══════════════════════════════════════════════════════ retired endpoints
@pytest.mark.asyncio
@pytest.mark.parametrize("path", [f"{API}/production/cutting", f"{API}/production/scan"])
async def test_the_pre_barcode_endpoints_report_gone(client, path):
    """410, not 404 — the frontend is told the route was retired, not mistyped
    (production/router.py:171-176)."""
    _as(UserRole.DIRECT_MANAGER)
    r = await client.post(path, json={})
    assert r.status_code == 410


# ══════════════════════════════════════════════════ superuser bypass, pinned
@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    f"{API}/wages/runs", f"{API}/employees", f"{API}/materials/stock",
])
@pytest.mark.parametrize("role", [UserRole.MANAGING_DIRECTOR, UserRole.DIRECT_MANAGER])
async def test_superusers_bypass_every_role_gate(client, role, path):
    """`SUPERUSER_ROLES` (users/deps.py:69) short-circuits `require_roles` before the
    allow-list is consulted. Deliberate today; the audit notes that it makes every
    gate in the matrix advisory for these two roles, and that DM is arguably an
    operational role that should not have it."""
    _as(role)
    r = await client.get(path)
    assert r.status_code == 200
