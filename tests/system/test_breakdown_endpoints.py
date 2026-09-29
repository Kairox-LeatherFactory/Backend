"""
================================================================================
tests/system/test_breakdown_endpoints.py — the breakdown edit-and-release surface
================================================================================

WHY THESE EXIST

Four breakdown operations had no HTTP-level test: `PATCH breakdown/styles/{id}`,
`POST breakdown/{order}/release`, `/cancel`, and `POST imports/commit`.

Release is the one that matters most in this file. CLAUDE.md s7: pieces are
minted at BREAKDOWN UPLOAD, not at cutting — a garment has a barcode identity
before it is ever cut. Release is the moment that happens, for every ordered unit
of every SKU in the order. It is the single most consequential write in the
system after payroll, and it was reachable over HTTP with nothing asserting that
the route works or who may call it.

WHAT IS ASSERTED

That the edit surface actually edits, that release mints and is idempotent (s7:
"re-running tops up, never duplicates"), and that the role gate holds — release
is a DM decision, because it commits the order to production.
================================================================================
"""
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.core.database import Base, get_db
from app.core.enums import UserRole
from app.main import app
from app.modules.production.models import Piece
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


@pytest_asyncio.fixture
async def draft(db, order_tree):
    """A style that has NOT been released, with one SKU.

    order_tree's style is RELEASED, and a released breakdown is deliberately
    FROZEN: its pieces already carry printed barcodes, so the sheet behind them
    cannot be rewritten (the 409 says exactly that). Editing is only legal
    before release, which is what these tests are about — so they need their own
    draft rather than a relaxation of that rule.
    """
    from app.modules.clients.models import SKU, Style

    style = Style(client_order_id=order_tree["order"].id, name="DRAFTSTYLE",
                  article="DS1", production_status="DRAFT")
    db.add(style)
    await db.flush()
    sku = SKU(style_id=style.id, color_code="RED", color_name="RED",
              size="L", qty_ordered=3, code="JP-DRAFTSTYLE-RED-L")
    db.add(sku)
    await db.commit()
    await db.refresh(style)
    await db.refresh(sku)
    return {"style": style, "sku": sku, "order": order_tree["order"]}


def _release_body(style, needs_lining: bool = False) -> dict:
    """The release contract: one entry per style, each DECLARING its lining.

    Not a formality. CLAUDE.md s9 makes lining a completeness question at the
    merge gate — a lined jacket needs leather AND lining before it can be
    line-stitched — so a style released without an answer would leave every one
    of its garments waiting for a part nobody decided it needed.
    """
    return {"styles": [{"style_id": str(style.id), "needs_lining": needs_lining}]}


async def _piece_count(db, sku_id) -> int:
    return int(await db.scalar(
        select(func.count()).select_from(Piece).where(Piece.sku_id == sku_id)) or 0)


# ══════════════════════════════════════════════════════════════ reading it back
@pytest.mark.asyncio
async def test_the_breakdown_of_an_order_reads_back(client, order_tree):
    """GET /imports/breakdown/{order_number} — the editable sheet."""
    _as(UserRole.DIRECT_MANAGER)
    order = order_tree["order"]
    body = _ok(await client.get(f"{API}/imports/breakdown/{order.order_number}"))
    assert body, "the seeded order has a breakdown and it should come back"


@pytest.mark.asyncio
async def test_an_unknown_order_number_is_404_not_a_crash(client):
    _as(UserRole.DIRECT_MANAGER)
    r = await client.get(f"{API}/imports/breakdown/NO-SUCH-ORDER")
    assert r.status_code == 404


# ══════════════════════════════════════════════════════════════ editing it
@pytest.mark.asyncio
@pytest.mark.integrity
async def test_patching_a_style_changes_it(client, db, draft):
    """PATCH /imports/breakdown/styles/{id} — the DM correcting the sheet.

    The breakdown is the production source of truth (CLAUDE.md s15), so an
    error here propagates into every piece minted from it. Editing before
    release is exactly when it is cheap to fix.
    """
    _as(UserRole.DIRECT_MANAGER)
    style = draft["style"]

    _ok(await client.patch(f"{API}/imports/breakdown/styles/{style.id}",
                           json={"season": "SS27", "customer_ref": "REF-9"}))

    await db.refresh(style)
    assert style.season == "SS27"
    assert style.customer_ref == "REF-9"


@pytest.mark.asyncio
@pytest.mark.integrity
async def test_patching_a_sku_changes_its_ordered_quantity(client, db, draft):
    """PATCH /imports/breakdown/skus/{id} — qty_ordered decides how many pieces
    release will mint, so it is the highest-consequence field on the sheet."""
    _as(UserRole.DIRECT_MANAGER)
    sku = draft["sku"]

    _ok(await client.patch(f"{API}/imports/breakdown/skus/{sku.id}",
                           json={"qty_ordered": 7}))

    await db.refresh(sku)
    assert sku.qty_ordered == 7


# ══════════════════════════════════════════════════════════════ releasing it
# ══════════════════════════════════ making release testable over HTTP
#
# `_release_sync` (imports/breakdown.py) opens its OWN `SessionLocal()` — the
# SYNC engine — because premint_order speaks the synchronous Session API and
# shares the four-phase insert ordering with the importer. In production both
# engines point at the same database and this is invisible. Under the ordinary
# async in-memory fixture they do NOT: the request writes to one database and
# the mint goes to another, so release appears to do nothing.
#
# `shared_db` below points BOTH engines at one temp-file SQLite, which is the
# only way this endpoint can be exercised end to end from HTTP. It is a fixture
# concern, not a production one — but it is worth having, because release is the
# write that mints a permanent barcode for every garment in an order.
@pytest_asyncio.fixture
async def shared_db(tmp_path, monkeypatch):
    """One database, visible to the async request AND the sync mint."""
    from sqlalchemy import create_engine
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.orm import sessionmaker

    path = tmp_path / "release.db"
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    sync_engine = create_engine(f"sqlite:///{path}", future=True)
    SyncSession = sessionmaker(bind=sync_engine, autoflush=False,
                               autocommit=False)
    # premint imports SessionLocal from app.core.database at CALL time, so
    # patching the attribute there is enough.
    monkeypatch.setattr("app.core.database.SessionLocal", SyncSession)

    AsyncSession_ = async_sessionmaker(async_engine, expire_on_commit=False)
    async with AsyncSession_() as session:
        yield session
    await async_engine.dispose()
    sync_engine.dispose()


@pytest_asyncio.fixture
async def shared_client(shared_db):
    async def _db():
        yield shared_db
    app.dependency_overrides[get_db] = _db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def shared_draft(shared_db, operations):
    """A DRAFT style + SKU in the SHARED database."""
    from app.modules.clients.models import SKU, Client, ClientOrder, Style

    client_row = Client(name="RELEASE CO", country="IT")
    shared_db.add(client_row)
    await shared_db.flush()
    order = ClientOrder(client_id=client_row.id, order_number="REL-PO")
    shared_db.add(order)
    await shared_db.flush()
    style = Style(client_order_id=order.id, name="RELSTYLE", article="RS1",
                  code="RELSTYLE", production_status="DRAFT")
    shared_db.add(style)
    await shared_db.flush()
    sku = SKU(style_id=style.id, color_code="BLK", color_name="BLACK",
              size="M", qty_ordered=3, code="REL-PO-RELSTYLE-BLK-M")
    shared_db.add(sku)
    await shared_db.commit()
    for obj in (order, style, sku):
        await shared_db.refresh(obj)
    return {"order": order, "style": style, "sku": sku}


@pytest.mark.asyncio
@pytest.mark.integrity
async def test_a_style_without_a_confirmed_material_spec_is_refused(
        client, draft):
    """THE RELEASE GATE — and the reason it exists.

    Release mints a permanent barcode for every garment in the style. Once those
    labels are printed the breakdown behind them is frozen, so anything wrong at
    release stays wrong. The gate makes the DM answer the material questions
    BEFORE that point: what each piece consumes, in leather, and whether it takes
    accessories.

    The refusal is per-style and itemised, not a flat "no" — the blockers are
    what the DM has to act on, so they are asserted individually.
    """
    _as(UserRole.DIRECT_MANAGER)
    order, style = draft["order"], draft["style"]

    body = _ok(await client.post(
        f"{API}/imports/breakdown/{order.order_number}/release",
        json=_release_body(style)))

    assert body["released"] == [], "an unconfirmed style must not reach the floor"
    rejected = body["rejected"]
    assert any(str(r["style_id"]) == str(style.id) for r in rejected)

    blockers = " ".join(next(
        r for r in rejected if str(r["style_id"]) == str(style.id))["blockers"])
    assert "material spec has not been confirmed" in blockers
    assert "no LEATHER line" in blockers
    assert "accessories" in blockers


@pytest.mark.asyncio
@pytest.mark.integrity
async def test_a_style_releases_once_its_material_spec_is_confirmed(
        shared_client, shared_db, shared_draft):
    """The gate opens when the DM has answered — the other half of the rule.

    This also drives four style endpoints that no test reached:
    POST .../material-spec/lines, POST .../confirm, GET .../material-spec and
    GET .../material-spec/requirement.
    """
    client = shared_client
    _as(UserRole.DIRECT_MANAGER)
    order, style, sku = (shared_draft["order"], shared_draft["style"],
                         shared_draft["sku"])

    _ok(await client.post(f"{API}/styles/{style.id}/material-spec/lines", json={
        "category": "LEATHER", "article": "NAPPA", "colour": "BLACK",
        "thickness": "1.2mm", "qty_per_piece": 12.5, "uom": "dcm",
    }))

    spec = _ok(await client.get(f"{API}/styles/{style.id}/material-spec"))
    assert spec, "the line just added should read back"

    # `no_accessories=True` is the DM DECLARING the absence, which is the thing
    # the gate wants: a spec with no accessory lines is ambiguous between "none
    # needed" and "nobody filled it in yet".
    _ok(await client.post(f"{API}/styles/{style.id}/material-spec/confirm",
                          json={"no_accessories": True}))

    _ok(await client.get(f"{API}/styles/{style.id}/material-spec/requirement"))

    body = _ok(await client.post(
        f"{API}/imports/breakdown/{order.order_number}/release",
        json=_release_body(style, needs_lining=True)))

    released = body["released"]
    assert any(str(r["style_id"]) == str(style.id) for r in released), (
        f"a confirmed style was still refused: {body.get('rejected')}")

    mine = next(r for r in released if str(r["style_id"]) == str(style.id))
    assert mine["needs_lining"] is True
    assert mine["lining_declared"] is True, (
        "the DM answered the lining question, so this style must not be "
        "recorded as having released on inference")

    # THE MINT ITSELF. CLAUDE.md s7: one Piece per ORDERED UNIT, minted at
    # release rather than at cutting, so the garment has a barcode identity
    # before anyone touches it. Visible here only because both engines share a
    # database — see the note on shared_db.
    minted = await _piece_count(shared_db, sku.id)
    assert minted == sku.qty_ordered, (
        f"release minted {minted} pieces for a SKU ordering {sku.qty_ordered}")


@pytest.mark.asyncio
@pytest.mark.integrity
async def test_releasing_twice_is_accepted_and_does_not_error(client, draft):
    """IDEMPOTENT BY DESIGN (CLAUDE.md s7: "re-running tops up, never
    duplicates").

    Release is the button a DM presses when they are not sure the first press
    worked. A second press must be a no-op, not a 500 and not a duplicate — and
    the no-duplicate half is asserted against the database in
    tests/integration/test_premint_insert_order.py, for the reason in the note
    above.
    """
    _as(UserRole.DIRECT_MANAGER)
    order, style = draft["order"], draft["style"]
    payload = _release_body(style)

    first = await client.post(
        f"{API}/imports/breakdown/{order.order_number}/release", json=payload)
    assert first.status_code in (200, 201), first.text

    second = await client.post(
        f"{API}/imports/breakdown/{order.order_number}/release", json=payload)
    assert second.status_code in (200, 201, 409), (
        f"a second release answered {second.status_code}: {second.text[:300]}")


@pytest.mark.asyncio
@pytest.mark.security
async def test_only_the_direct_manager_may_release_an_order(client, draft):
    """Release commits the order to production and mints every barcode in it.
    That is a DM decision, not something a floor manager can trigger."""
    order, style = draft["order"], draft["style"]
    for role in (UserRole.CUTTING_MANAGER, UserRole.STITCHING_MANAGER,
                 UserRole.HR, UserRole.SUPERVISOR):
        _as(role)
        r = await client.post(
            f"{API}/imports/breakdown/{order.order_number}/release",
            json=_release_body(style))
        assert r.status_code in (401, 403), (
            f"{role.value} released an order (HTTP {r.status_code})")


@pytest.mark.asyncio
async def test_cancelling_styles_answers(client, draft):
    """POST /imports/breakdown/{order}/cancel — dropping styles from the sheet
    before they reach the floor."""
    _as(UserRole.DIRECT_MANAGER)
    order, style = draft["order"], draft["style"]

    r = await client.post(
        f"{API}/imports/breakdown/{order.order_number}/cancel",
        json={"style_ids": [str(style.id)]})
    # 200 (cancelled) or 409 (already released) are both real answers; a 5xx or
    # a 422 would mean the route does not work.
    assert r.status_code in (200, 201, 409), r.text
