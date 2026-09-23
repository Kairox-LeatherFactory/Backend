"""
================================================================================
tests/system/test_inspection_endpoints.py — reject, rework and who decides
================================================================================

WHY THESE EXIST

Three inspection operations had no HTTP-level test: `approve`, `decline` and
`pieces/{piece_code}`.

An inspection is where a garment gets sent back. The decision costs real money —
the stage is walked again, the second pass is rework, and rework is priced
separately so "this style cost X, of which Y was defects" stays answerable. Who
may make that decision is therefore a role question, and a role question is only
visible over HTTP.
================================================================================
"""
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.database import get_db
from app.core.enums import UserRole
from app.main import app
from app.modules.users.deps import get_current_user

from tests.system.test_role_guards import FakeUser

API = "/api/v1"


@pytest_asyncio.fixture
async def client(db):
    async def _db():
        yield db
    app.dependency_overrides[get_db] = _db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def _as(role: UserRole):
    app.dependency_overrides[get_current_user] = lambda: FakeUser(role)


def _ok(response):
    assert response.status_code in (200, 201), (
        f"{response.request.method} {response.request.url.path} -> "
        f"{response.status_code}: {response.text[:300]}")
    return response.json()


# ══════════════════════════════════════════════════════════════════ reading
@pytest.mark.asyncio
async def test_the_inspection_list_answers(client):
    """GET /inspections — the DM's queue of decisions to make."""
    _as(UserRole.DIRECT_MANAGER)
    body = _ok(await client.get(f"{API}/inspections"))
    assert isinstance(body, (list, dict))


@pytest.mark.asyncio
async def test_an_unknown_piece_barcode_is_404_not_a_crash(client):
    """GET /inspections/pieces/{code} — the per-garment history.

    A barcode that does not resolve is a 404 with a message naming the code, not
    a stack trace. On the floor this is a mistyped or damaged label, which is
    routine.
    """
    _as(UserRole.DIRECT_MANAGER)
    r = await client.get(f"{API}/inspections/pieces/NO-SUCH-BARCODE")
    assert r.status_code == 404
    assert "NO-SUCH-BARCODE" in r.text


@pytest.mark.asyncio
async def test_the_responsibility_report_answers(client):
    """GET /inspections/responsibility — defects grouped by who was responsible.

    Read-only and blame-adjacent, which is exactly why it is a report rather
    than something that feeds the wage run automatically.
    """
    _as(UserRole.DIRECT_MANAGER)
    body = _ok(await client.get(f"{API}/inspections/responsibility"))
    assert isinstance(body, list)


# ══════════════════════════════════════════════════════════════ the decision
@pytest.mark.asyncio
async def test_approving_an_unknown_inspection_is_404(client):
    """POST /inspections/{id}/approve on an id that does not exist.

    The decision endpoints had no HTTP test at all, so this pins the basic
    contract: they resolve their target, and they answer rather than raise when
    it is missing.
    """
    import uuid
    _as(UserRole.DIRECT_MANAGER)
    r = await client.post(f"{API}/inspections/{uuid.uuid4()}/approve", json={})
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_declining_an_unknown_inspection_is_404(client):
    import uuid
    _as(UserRole.DIRECT_MANAGER)
    r = await client.post(f"{API}/inspections/{uuid.uuid4()}/decline", json={})
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
@pytest.mark.security
async def test_the_floor_cannot_approve_its_own_rework(client):
    """WHO SIGNS OFF A REJECTION IS A SEPARATION-OF-DUTIES QUESTION.

    The stitching manager whose line produced the defect must not also be the
    one who approves the rework that hides it. The gate is what enforces that,
    and only a request can see the gate.
    """
    import uuid
    inspection_id = uuid.uuid4()
    for role in (UserRole.STITCHING_MANAGER, UserRole.CUTTING_MANAGER,
                 UserRole.SUPERVISOR, UserRole.VIEWER):
        _as(role)
        r = await client.post(
            f"{API}/inspections/{inspection_id}/approve", json={})
        # 403 is the gate refusing. 404 means the gate let them through and the
        # id simply does not exist — which is the failure this test is for.
        assert r.status_code in (401, 403), (
            f"{role.value} reached the approve decision (HTTP {r.status_code}) "
            "— approving rework is not a floor-manager decision")
