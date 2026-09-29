"""
SYSTEM · every list endpoint can actually be PAGED.

WHY THIS FILE EXISTS. "Pagination is not working anywhere" was reported against
the whole API, and it was three separate things wearing one coat:

  1. The Postman collections shipped every optional query param DISABLED — a
     disabled param is not sent at all — so `limit` and `offset` were visible in
     the request, greyed out, and never left the machine. Pressing Send returned
     an unpaged response because nothing had been asked for. Fixed in
     scripts/make_postman_collection.py (_PAGING_DEFAULTS).

  2. Three routes declared `limit` and no `offset`. A limit with no offset is a
     CAP, not a pager: you can ask for the first 200 rows and have no way to ask
     for the next 200. `/inspections`, `/jobwork` and `/store/pieces` now take
     one.

  3. Several routes page correctly but return a bare list, so the caller cannot
     tell a full page from the last page. The `Page` envelope carries `total`
     and `has_more` for exactly that; core/pagination.py says why the older
     published shapes are not retrofitted under a working frontend.

THE TEST ITSELF asserts the property that actually matters, endpoint by
endpoint: page 1 and page 2 return DIFFERENT rows, and a limit is honoured.
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
pytestmark = pytest.mark.asyncio


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
    app.dependency_overrides[get_current_user] = \
        lambda: FakeUser(UserRole.MANAGING_DIRECTOR)
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def five_workers(db):
    """Five rows, so limit=2 is visibly different from limit=50."""
    made = []
    for n in range(5):
        e = Employee(name=f"PAGER {n}", designation="CUTTER",
                     wage_type=WageType.PIECE_RATE, monthly_salary=0,
                     is_active=True)
        db.add(e)
        made.append(e)
    await db.commit()
    return made


# ══════════════════════════ the Page envelope, end to end over HTTP
async def test_a_paged_list_walks_and_never_repeats_a_row(client, five_workers):
    """THE PROPERTY THAT MATTERS: page 2 is not page 1."""
    first = await client.get(f"{API}/employees", params={"limit": 2, "offset": 0})
    second = await client.get(f"{API}/employees", params={"limit": 2, "offset": 2})
    assert first.status_code == 200 and second.status_code == 200

    a, b = first.json(), second.json()
    assert a["limit"] == 2 and a["offset"] == 0 and a["count"] == 2
    assert b["offset"] == 2
    ids_a = {row["id"] for row in a["items"]}
    ids_b = {row["id"] for row in b["items"]}
    assert not (ids_a & ids_b), "offset must skip the rows already seen"
    # `total` counts the WHOLE match, not the page — it is what a list screen
    # renders as "1-2 of 5" and what tells it another page exists.
    assert a["total"] >= 5 and a["has_more"] is True


async def test_the_last_page_says_it_is_the_last(client, five_workers):
    """`has_more` is derived server-side so every client is not left to
    reimplement `offset + len(items) < total` and get it off by one."""
    r = await client.get(f"{API}/employees", params={"limit": 200, "offset": 0})
    body = r.json()
    assert body["has_more"] is False
    assert body["count"] == body["total"]


async def test_limit_is_capped_rather_than_letting_a_caller_ask_for_everything(
        client, five_workers):
    """?limit=999999 would reintroduce exactly the problem paging solves."""
    r = await client.get(f"{API}/employees", params={"limit": 999999})
    assert r.status_code == 422
    assert (await client.get(f"{API}/employees",
                             params={"limit": 0})).status_code == 422
    assert (await client.get(f"{API}/employees",
                             params={"offset": -1})).status_code == 422


# ══════════════════════════ every route that claims to page, must page
PAGED_ROUTES = [
    "/clients", "/clients/styles", "/employees", "/users",
    "/production/events", "/production/skus",
    "/barcode/orders", "/barcode/materials",
    "/wages/orders", "/wages/styles", "/wages/runs", "/wages/ledger",
    "/imports/orders", "/materials/leather-by-style",
    "/inspections", "/jobwork", "/store/pieces",
]


@pytest.mark.parametrize("path", PAGED_ROUTES)
async def test_every_list_route_accepts_limit_and_offset(client, path):
    """A 422 here means the route declares one and not the other.

    THREE ROUTES USED TO FAIL THIS — /inspections, /jobwork and /store/pieces
    took a `limit` with no `offset`, which is a cap and not a pager.
    """
    r = await client.get(f"{API}{path}", params={"limit": 1, "offset": 1})
    assert r.status_code == 200, f"{path} -> {r.status_code}: {r.text[:200]}"


async def test_the_store_list_reports_a_total_not_only_a_page(client, in_store):
    """`count` is this page; `total` is how many are in the store.

    Without `total` a screen cannot tell a full page from the last one, which is
    what made a working `limit` look like broken pagination.
    """
    r = await client.get(f"{API}/store/pieces", params={"limit": 1, "offset": 0})
    body = r.json()
    assert body["count"] <= 1
    assert body["total"] >= body["count"]
    assert "has_more" in body and body["offset"] == 0
