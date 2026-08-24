"""
SYSTEM / E2E · the material-spec surface through the real app.

TWO THINGS THIS LAYER CAN SEE AND THE OTHERS CANNOT
    1. WHO MAY REACH WHAT. Every gate here is a Depends(...), and a dependency
       attached to the wrong router or shadowed by an earlier path is invisible
       to a service-level test. Reads are deliberately open to the whole floor —
       the cutting manager needs the dcm and the store needs the accessory list —
       while writes are DM/MD.
    2. THE ADDITIVITY CONTRACT. This feature promised the frontend that nothing
       it reads today changes. `test_barcode_resolve_keeps_every_key_it_had`
       machine-checks that promise instead of trusting a code review.
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
    def __init__(self, role):
        self.id = uuid.uuid4()
        self.role = role
        self.name = role.value
        self.phone = "9000000000"
        self.employee_id = None
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


def _as(role):
    app.dependency_overrides[get_current_user] = lambda: FakeUser(role)


# ══════════════════════════════════════════════════════════ who may read
@pytest.mark.asyncio
@pytest.mark.parametrize("role", [
    UserRole.DIRECT_MANAGER, UserRole.MANAGING_DIRECTOR,
    UserRole.CUTTING_MANAGER, UserRole.STITCHING_MANAGER,
    UserRole.STORE_MANAGER,
])
async def test_the_floor_may_read_the_recipe(client, order_tree, role):
    """The cutting manager needs the dcm and the store needs the accessory list.
    A recipe only DM/MD could read would be a checklist nobody at the drawer
    could see, which is the problem this feature exists to solve."""
    _as(role)
    r = await client.get(
        f"{API}/styles/{order_tree['style'].id}/material-spec")
    assert r.status_code == 200, r.text
    assert "release_blockers" in r.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.CLIENT])
async def test_read_only_roles_are_refused(client, order_tree, role):
    _as(role)
    r = await client.get(f"{API}/styles/{order_tree['style'].id}/material-spec")
    assert r.status_code == 403


# ══════════════════════════════════════════════════════════ who may write
@pytest.mark.asyncio
@pytest.mark.parametrize("role,expected", [
    (UserRole.DIRECT_MANAGER, {200, 409}),      # 409 = the style is RELEASED
    (UserRole.MANAGING_DIRECTOR, {200, 409}),
    (UserRole.CUTTING_MANAGER, {403}),
    (UserRole.STORE_MANAGER, {403}),
    (UserRole.STITCHING_MANAGER, {403}),
    (UserRole.HR, {403}),
])
async def test_only_dm_and_md_may_write_the_recipe(client, order_tree, role,
                                                   expected):
    _as(role)
    r = await client.put(
        f"{API}/styles/{order_tree['style'].id}/material-spec",
        json={"lines": []})
    assert r.status_code in expected, f"{role.value} got {r.status_code}: {r.text}"


@pytest.mark.asyncio
async def test_confirming_is_a_dm_md_action(client, order_tree):
    _as(UserRole.CUTTING_MANAGER)
    r = await client.post(
        f"{API}/styles/{order_tree['style'].id}/material-spec/confirm",
        json={"no_accessories": True})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_the_store_manager_may_record_an_off_spec_issue(client):
    """The correction is made AT THE DRAWER, by the store. Making them wait for a
    DM is how the correction stops being recorded at all — and an unrecorded
    issue is exactly the fiction this feature removes."""
    _as(UserRole.STORE_MANAGER)
    r = await client.post(f"{API}/materials/issues",
                          json={"qty": 1})          # missing ids → 422, not 403
    assert r.status_code == 422

    _as(UserRole.STITCHING_MANAGER)
    r = await client.post(f"{API}/materials/issues", json={"qty": 1})
    assert r.status_code == 403


# ═══════════════════════════════════════════ THE ADDITIVITY CONTRACT
# The promise made to the two frontend devs was that this feature adds keys and
# removes none. These two tests are that promise, machine-checked — the key sets
# below are what the API returned BEFORE the material spec existed.

_PIECE_KEYS_BEFORE = {
    "piece_id", "code", "short_code", "sku_id", "sku_code", "style_id",
    "style_name", "article", "serial", "colour", "size", "seq", "order_id",
    "order_number", "client", "current_stage", "drawer_code", "drawer",
    "leather_consumption_dcm", "needs_lining", "label_line",
}
_DRAWER_KEYS_BEFORE = {
    "drawer_id", "drawer_code", "seq", "state", "current_piece_id",
    "leather_in", "lining_in", "holding",
}
_STORE_SCAN_KEYS_BEFORE = {
    "drawer_code", "piece_code", "state", "needs_lining", "lining_reason",
    "awaiting", "ready_for_received", "part", "part_inferred", "employee_id",
    "holding", "auto_received", "sent", "next_action",
}


@pytest.mark.asyncio
async def test_barcode_resolve_keeps_every_key_it_had(client, pieces):
    piece, drawer = pieces[0]
    _as(UserRole.DIRECT_MANAGER)

    r = await client.get(f"{API}/barcode/resolve", params={"code": piece.code})
    assert r.status_code == 200, r.text
    body = r.json()["piece"]
    assert _PIECE_KEYS_BEFORE <= set(body), (
        "the piece payload LOST keys a current client may be reading: "
        f"{sorted(_PIECE_KEYS_BEFORE - set(body))}")
    assert "material_requirement" in body          # and gained exactly this

    r = await client.get(f"{API}/barcode/resolve", params={"code": drawer.code})
    assert r.status_code == 200, r.text
    body = r.json()["drawer"]
    assert _DRAWER_KEYS_BEFORE <= set(body), (
        "the drawer payload LOST keys: "
        f"{sorted(_DRAWER_KEYS_BEFORE - set(body))}")
    assert {"accessories_in", "material_requirement"} <= set(body)


@pytest.mark.asyncio
async def test_a_style_with_no_spec_reports_not_required_not_an_empty_checklist(
        client, pieces):
    """The difference the screen depends on: NOT_REQUIRED means hide the panel,
    PENDING means show it with outstanding counts. Every style released before
    this feature must read NOT_REQUIRED."""
    piece, _drawer = pieces[0]
    _as(UserRole.DIRECT_MANAGER)
    r = await client.get(f"{API}/barcode/resolve", params={"code": piece.code})
    block = r.json()["piece"]["material_requirement"]
    assert block["kit_status"] == "NOT_REQUIRED"
    assert block["kit_required"] is False
    assert block["accessories"] == []
