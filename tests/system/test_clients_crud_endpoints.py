"""
SYSTEM / E2E · the client edit + delete endpoints over HTTP.

Two things only this layer can see:

  1. ROUTE ORDER. `GET /clients/{client_id}` and `GET /clients/styles` are both
     registered on the same router, and FastAPI matches in REGISTRATION order.
     Declared in the wrong order, `/clients/styles` is swallowed by the UUID
     route and 422s — with the service layer none the wiser, because the service
     is never reached. One test below exists purely to keep that from regressing.

  2. THE ROLE GATE. Editing and deleting a client are DM-only, matching client
     creation. A `Depends` that is declared but attached to the wrong route is
     invisible to a service-level test.
"""
import uuid
from datetime import date

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.database import get_db
from app.core.enums import UserRole
from app.main import app
from app.modules.clients.models import Client
from app.modules.production.models import Piece
from app.modules.users.deps import get_current_user

API = "/api/v1"

pytestmark = pytest.mark.security


class FakeUser:
    def __init__(self, role, client_id=None):
        self.id = uuid.uuid4()
        self.role = role
        self.name = role.value
        self.phone = "9000000000"
        self.employee_id = None
        self.client_id = client_id
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


def _as(role, **kw):
    app.dependency_overrides[get_current_user] = lambda: FakeUser(role, **kw)


async def _bare_client(db, name="ACME"):
    c = Client(name=name)
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


# ══════════════════════════════════════════════════════════════ route order
@pytest.mark.asyncio
async def test_the_styles_path_is_not_swallowed_by_the_uuid_route(client, db):
    """REGRESSION GUARD. `/clients/styles` must keep resolving to the styles
    handler now that `/clients/{client_id}` exists. If the UUID route is ever
    moved above it, this returns 422 ("styles" is not a UUID) instead of 200."""
    _as(UserRole.DIRECT_MANAGER)
    r = await client.get(f"{API}/clients/styles")
    assert r.status_code == 200, r.text
    # Paged (core/pagination.py) — the rows are under `items`. The point of this
    # guard is the ROUTE, not the shape: a 422 here means "styles" was parsed as
    # a client_id.
    assert isinstance(r.json()["items"], list)


# ══════════════════════════════════════════════════════════════════ read/edit
@pytest.mark.asyncio
async def test_a_dm_reads_then_edits_a_client(client, db):
    """The round trip an edit form makes: GET to populate, PATCH to save."""
    c = await _bare_client(db, name="BEFORE")
    _as(UserRole.DIRECT_MANAGER)

    got = await client.get(f"{API}/clients/{c.id}")
    assert got.status_code == 200
    assert got.json()["name"] == "BEFORE"

    saved = await client.patch(f"{API}/clients/{c.id}",
                               json={"name": "AFTER", "country": "FR"})
    assert saved.status_code == 200, saved.text
    assert saved.json()["name"] == "AFTER"
    assert saved.json()["country"] == "FR"


@pytest.mark.asyncio
async def test_a_non_dm_cannot_edit_or_delete(client, db):
    """Editing and deleting sit at the same authority as creating."""
    c = await _bare_client(db, name="PROTECTED")
    for role in (UserRole.HR, UserRole.SUPERVISOR, UserRole.CUTTING_MANAGER,
                 UserRole.VIEWER):
        _as(role)
        assert (await client.patch(f"{API}/clients/{c.id}",
                                   json={"name": "X"})).status_code == 403
        assert (await client.delete(f"{API}/clients/{c.id}")).status_code == 403


@pytest.mark.asyncio
async def test_a_client_login_cannot_read_another_clients_record(client, db):
    """Mirrors the ownership check already on /{client_id}/orders — otherwise
    the new single-read endpoint would be a way around it."""
    mine = await _bare_client(db, name="MINE")
    theirs = await _bare_client(db, name="THEIRS")

    _as(UserRole.CLIENT, client_id=mine.id)
    assert (await client.get(f"{API}/clients/{mine.id}")).status_code == 200
    assert (await client.get(f"{API}/clients/{theirs.id}")).status_code == 403


@pytest.mark.asyncio
async def test_editing_an_unknown_client_is_404(client, db):
    _as(UserRole.DIRECT_MANAGER)
    r = await client.patch(f"{API}/clients/{uuid.uuid4()}", json={"name": "X"})
    assert r.status_code == 404


# ════════════════════════════════════════════════════════════════════ delete
@pytest.mark.asyncio
async def test_deleting_a_client_with_no_orders_is_204(client, db):
    c = await _bare_client(db, name="DISPOSABLE")
    _as(UserRole.DIRECT_MANAGER)

    r = await client.delete(f"{API}/clients/{c.id}")
    assert r.status_code == 204, r.text
    assert (await client.get(f"{API}/clients/{c.id}")).status_code == 404


@pytest.mark.asyncio
async def test_deleting_a_client_with_pieces_is_409(client, db, order_tree):
    """The refusal a DM will actually hit, with the deactivate call in the body
    so the frontend can offer it as the next step.

    It takes a MINTED PIECE to earn the refusal. An order on its own no longer
    does, because `POST /clients` creates one every time — see the test below."""
    db.add(Piece(code="JP-CLERMONT-PINE-M-001", seq=1,
                 sku_id=order_tree["sku"].id))
    await db.commit()
    _as(UserRole.DIRECT_MANAGER)

    r = await client.delete(f"{API}/clients/{order_tree['client'].id}")
    assert r.status_code == 409, r.text
    assert "is_active" in r.json()["detail"]


@pytest.mark.asyncio
async def test_a_client_can_be_deleted_right_after_being_created(client, db):
    """END-TO-END REGRESSION. Create a client the only way the API allows, then
    delete it. `POST /clients` requires an order_number and mints the order in
    the same call, so under the old orders-based guard this exact sequence —
    the mistyped-client cleanup the endpoint exists for — returned
    "has 1 order(s) and cannot be deleted" every single time."""
    _as(UserRole.DIRECT_MANAGER)

    made = await client.post(f"{API}/clients", json={
        "name": "TYPOO", "country": "IT", "order_number": "TYPO-PO-1"})
    assert made.status_code == 201, made.text
    client_id = made.json()["id"]

    r = await client.delete(f"{API}/clients/{client_id}")
    assert r.status_code == 204, r.text
    assert (await client.get(f"{API}/clients/{client_id}")).status_code == 404


@pytest.mark.asyncio
async def test_deactivating_removes_a_client_from_the_default_listing(client, db):
    """The documented alternative to deleting, end to end."""
    c = await _bare_client(db, name="RETIRING")
    _as(UserRole.DIRECT_MANAGER)

    assert (await client.patch(f"{API}/clients/{c.id}",
                               json={"is_active": False})).status_code == 200

    # GET /clients is paged (core/pagination.py), so the rows are in `items`.
    listed = await client.get(f"{API}/clients")
    assert "RETIRING" not in {row["name"] for row in listed.json()["items"]}

    everyone = await client.get(f"{API}/clients", params={"include_inactive": True})
    assert "RETIRING" in {row["name"] for row in everyone.json()["items"]}


# ═══════════════════════════ what BLOCKS a delete, named rather than guessed
#
# The refusal used to say "other records (a style rate, a supplier PO, a cutting
# entry) still reference their styles" — three guesses, none of them confirmed,
# and nothing the DM could act on. The blocking rows are knowable: they are the
# FKs into the client's styles and SKUs that carry no `ondelete`.
@pytest.mark.asyncio
async def test_an_order_and_a_style_alone_do_not_block_a_delete(client, db,
                                                                order_tree):
    """`POST /clients` mints an order every time and a breakdown adds styles.

    Neither is evidence the client has traded, and both cascade away with them.
    Treating them as blockers is what made this endpoint impossible to pass.
    """
    _as(UserRole.DIRECT_MANAGER)
    check = await client.get(f"{API}/clients/{order_tree['client'].id}/deletable")
    assert check.status_code == 200, check.text
    assert check.json()["deletable"] is True
    assert check.json()["blockers"] == []


@pytest.mark.asyncio
async def test_the_preflight_names_the_row_that_blocks(client, db, order_tree):
    """A minted garment blocks — and the DM is told which table and how many."""
    db.add(Piece(code="JP-CLERMONT-PINE-M-001", seq=1,
                 sku_id=order_tree["sku"].id))
    await db.commit()
    _as(UserRole.DIRECT_MANAGER)

    check = await client.get(f"{API}/clients/{order_tree['client'].id}/deletable")
    body = check.json()
    assert body["deletable"] is False
    assert [b["table"] for b in body["blockers"]] == ["piece"]
    assert body["blockers"][0]["count"] == 1
    assert "is_active" in body["alternative"]

    # The preflight and the refusal must never disagree about the rule.
    refused = await client.delete(f"{API}/clients/{order_tree['client'].id}")
    assert refused.status_code == 409
    assert "1 garment(s)" in refused.json()["detail"]


@pytest.mark.asyncio
async def test_a_wage_rate_on_a_style_blocks_even_with_no_garments(
        client, db, order_tree):
    """THE CASE THE OLD GUARD MISSED, and why the IntegrityError arm existed.

    `rate.style_id` has no `ondelete`, so Postgres refuses the cascade — but the
    piece count said zero, so the DM was told the delete would work and then got
    an unexplained 409. Now it is counted up front and named.
    """
    from decimal import Decimal
    from app.modules.wages.models import Rate
    from app.modules.production.models import Operation
    op = Operation(code="LEATHER_CUTTING", label="Leather cutting", sequence=1)
    db.add(op)
    await db.flush()
    db.add(Rate(style_id=order_tree["style"].id, operation_id=op.id,
                rate=Decimal("12.50"), effective_from=date.today()))
    await db.commit()
    _as(UserRole.DIRECT_MANAGER)

    body = (await client.get(
        f"{API}/clients/{order_tree['client'].id}/deletable")).json()
    assert body["deletable"] is False
    assert [b["table"] for b in body["blockers"]] == ["rate"]

    refused = await client.delete(f"{API}/clients/{order_tree['client'].id}")
    assert refused.status_code == 409
    assert "wage rate card" in refused.json()["detail"]
